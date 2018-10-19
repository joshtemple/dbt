import abc
import copy
import multiprocessing
import time

import agate
import six

import dbt.exceptions
import dbt.flags
import dbt.schema
import dbt.clients.agate_helper

from dbt.compat import abstractclassmethod, classmethod
from dbt.contracts.connection import Connection
from dbt.logger import GLOBAL_LOGGER as logger
from dbt.schema import Column
from dbt.utils import filter_null_values

from dbt.adapters.base.meta import AdapterMeta, available, available_raw
from dbt.adapters.base import BaseRelation
from dbt.adapters.cache import RelationsCache

GET_CATALOG_OPERATION_NAME = 'get_catalog_data'


def _expect_row_value(key, row):
    if key not in row.keys():
        raise dbt.exceptions.InternalException(
            'Got a row without "{}" column, columns: {}'
            .format(key, row.keys())
        )
    return row[key]


def _relations_filter_schemas(schemas):
    def test(row):
        referenced_schema = _expect_row_value('referenced_schema', row)
        dependent_schema = _expect_row_value('dependent_schema', row)
        # handle the null schema
        if referenced_schema is not None:
            referenced_schema = referenced_schema.lower()
        if dependent_schema is not None:
            dependent_schema = dependent_schema.lower()
        return referenced_schema in schemas or dependent_schema in schemas
    return test


def _catalog_filter_schemas(manifest):
    """Return a function that takes a row and decides if the row should be
    included in the catalog output.
    """
    schemas = frozenset(s.lower() for s in manifest.get_used_schemas())

    def test(row):
        table_schema = _expect_row_value('table_schema', row)
        # the schema may be present but None, which is not an error and should
        # be filtered out
        if table_schema is None:
            return False
        return table_schema.lower() in schemas
    return test


@six.add_metaclass(AdapterMeta)
class BaseAdapter(object):
    """The BaseAdapter provides an abstract base class for adapters.

    Adapters must implement the following methods. Some of these methods can be
    safely overridden as a noop, where it makes sense (transactions on
    databases that don't support them, for instance). Those methods are marked
    with a (passable) in their docstrings. Check docstrings for type
    information, etc.

    Methods:
        - exception_handler
        - date_function
        - get_existing_schemas
        - drop_relation
        - truncate_relation
        - rename_relation
        - get_columns_in_relation
        - expand_column_types
        - list_relations_without_caching
        - is_cancelable
        - create_schema
        - drop_schema
        - quote
        - convert_text_type
        - convert_number_type
        - convert_boolean_type
        - convert_datetime_type
        - convert_date_type
        - convert_time_type
    """
    requires = {}

    Relation = BaseRelation
    Column = Column
    # This should be an implementation of BaseConnectionManager
    ConnectionManager = None

    def __init__(self, config):
        self.config = config
        self.cache = RelationsCache()
        self.connections = self.ConnectionManager(config)

    ###
    # Methods that pass through to the connection manager
    ###
    # TODO: I would like a better way to do this. Should adapters be subclasses
    # of managers?
    # TODO: Remove this
    def acquire_connection(self, name):
        return self.connections.get(name)

    def release_connection(self, name):
        return self.connections.release(name)

    def cleanup_connections(self):
        return self.connections.cleanup_all()

    def clear_transaction(self, conn_name='master'):
        return self.connections.clear_transaction(conn_name)

    def commit_if_has_connection(self, name):
        return self.connections.commit_if_has_connection(name)

    @available
    def execute(self, sql, model_name=None, auto_begin=False, fetch=False):
        """Execute the given SQL. This is a thin wrapper around
        ConnectionManager.execute.

        :param str sql: The sql to execute.
        :param Optional[str] model_name: The model name to use for the
            connection.
        :param bool auto_begin: If set, and dbt is not currently inside a
            transaction, automatically begin one.
        :param bool fetch: If set, fetch results.
        :return: A tuple of the status and the results (empty if fetch=False).
        :rtype: Tuple[str, agate.Table]
        """
        return self.connections.execute(
            sql=sql,
            name=model_name,
            auto_begin=auto_begin,
            fetch=fetch
        )

    ###
    # Methods that should never be overridden
    ###
    @classmethod
    def type(cls):
        """Get the type of this adapter. Types must be class-unique and
        consistent.

        :return: The type name
        :rtype: str
        """
        return cls.ConnectionManager.TYPE

    ###
    # Caching methods
    ###
    def _schema_is_cached(self, schema, model_name=None):
        """Check if the schema is cached, and by default logs if it is not."""
        if dbt.flags.USE_CACHE is False:
            return False
        elif schema not in self.cache:
            logger.debug(
                'On "{}": cache miss for schema "{}", this is inefficient'
                .format(model_name or '<None>', schema)
            )
            return False
        else:
            return True

    @classmethod
    def _relations_filter_table(cls, table, schemas):
        """Filter the table as appropriate for relations table entries.
        Subclasses can override this to change filtering rules on a per-adapter
        basis.
        """
        return table.where(_relations_filter_schemas(schemas))

    def _relations_cache_for_schemas(self, manifest):
        """Populate the relations cache for the given schemas. Returns an
        iteratble of the schemas populated, as strings.
        """
        if not dbt.flags.USE_CACHE:
            return

        schemas = manifest.get_used_schemas()

        relations = []
        # add all relations
        for schema in schemas:
            for relation in self.list_relations_without_caching(schema):
                self.cache.add(relation)
        # it's possible that there were no relations in some schemas. We want
        # to insert the schemas we query into the cache's `.schemas` attribute
        # so we can check it later
        self.cache.update_schemas(schemas)

    def set_relations_cache(self, manifest, clear=False):
        """Run a query that gets a populated cache of the relations in the
        database and set the cache on this adapter.
        """
        if not dbt.flags.USE_CACHE:
            return

        with self.cache.lock:
            if clear:
                self.cache.clear()
            self._relations_cache_for_schemas(manifest)

    @available
    def cache_new_relation(self, relation, model_name=None):
        """Cache a new relation in dbt. It will show up in `list relations`."""
        if relation is None:
            dbt.exceptions.raise_compiler_error(
                'Attempted to cache a null relation for {}'.format(model_name)
            )
        if dbt.flags.USE_CACHE:
            self.cache.add(relation)
        # so jinja doesn't render things
        return ''

    ###
    # Abstract methods for database-specific values, attributes, and types
    ###
    # TODO: should this be an abstract property?
    @abstractclassmethod
    def date_function(cls):
        """Get the date function used by this adapter's database.

        :return: The date function
        :rtype: str
        """
        raise dbt.exceptions.NotImplementedException(
            '`date_function` is not implemented for this adapter!')

    @abstractclassmethod
    def is_cancelable(cls):
        raise dbt.exceptions.NotImplementedException(
            '`is_cancelable` is not implemented for this adapter!'
        )

    def get_default_schema(self):
        return self.config.credentials.schema

    ###
    # Abstract methods about schemas
    ###
    # TODO: rename to `list_schemas` for consistency w/ realtions
    @abc.abstractmethod
    def get_existing_schemas(self, model_name=None):
        """Get a list of existing schemas.

        :param Optional[str] model_name: The name of the connection to query as
        :return: All schemas that currently exist in the database
        :rtype: List[str]
        """
        raise dbt.exceptions.NotImplementedException(
            '`get_existing_schemas` is not implemented for this adapter!'
        )

    def check_schema_exists(self, schema, model_name=None):
        """Check if a schema exists.

        The default implementation of this is potentially unnecessarily slow,
        and adapters should implement it if there is an optimized path (and
        there probably is)
        """
        search = (
            s.lower() for s in
            self.get_existing_schemas(model_name=model_name)
        )
        return schema.lower() in search

    ###
    # Abstract methods about relations
    ###
    @abc.abstractmethod
    @available
    def drop_relation(self, relation, model_name=None):
        """Drop the given relation.

        *Implementors must call self.cache.drop() to preserve cache state!*

        :param self.Relation relation: The relation to drop
        :param Optional[str] model_name: The name of the model to use for the
            connection.
        """
        raise dbt.exceptions.NotImplementedException(
            '`drop_relation` is not implemented for this adapter!'
        )

    @abc.abstractmethod
    @available
    def truncate_relation(self, relation, model_name=None):
        """Truncate the given relation.

        :param self.Relation relation: The relation to truncate
        :param Optional[str] model_name: The name of the model to use for the
            connection."""
        raise dbt.exceptions.NotImplementedException(
            '`truncate_relation` is not implemented for this adapter!'
        )

    @abc.abstractmethod
    @available
    def rename_relation(self, from_relation, to_relation, model_name=None):
        """Rename the relation from from_relation to to_relation.

        Implementors must call self.cache.rename() to preserve cache state.

        :param self.Relation from_relation: The original relation name
        :param self.Relation to_relation: The new relation name
        :param Optional[str] model_name: The name of the model to use for the
            connection.
        """
        raise dbt.exceptions.NotImplementedException(
            '`rename_relation` is not implemented for this adapter!'
        )

    @abc.abstractmethod
    @available
    def get_columns_in_relation(self, relation, model_name=None):
        """Get a list of the columns in the given Relation.

        :param self.Relation relation: The relation to query for.
        :param Optional[str] model_name: The name of the model to use for the
            connection.
        :return: Information about all columns in the given relation.
        :rtype: List[self.Column]
        """
        raise dbt.exceptions.NotImplementedException(
            '`get_columns_in_relation` is not implemented for this adapter!'
        )

    @abc.abstractmethod
    def expand_column_types(self, goal, current, model_name=None):
        """Expand the current table's types to match the goal table. (passable)

        :param self.Relation goal: A relation that currently exists in the
            database with columns of the desired types.
        :param self.Relation current: A relation that currently exists in the
            database with columns of unspecified types.
        :param Optional[str] model_name: The name of the model to use for the
            connection.
        """
        raise dbt.exceptions.NotImplementedException(
            '`expand_target_column_types` is not implemented for this adapter!'
        )

    @abc.abstractmethod
    def list_relations_without_caching(self, schema, model_name=None):
        """List relations in the given schema, bypassing the cache.

        This is used as the underlying behavior to fill the cache.

        :param str schema: The name of the schema to list relations from.
        :param Optional[str] model_name: The name of the model to use for the
            connection.
        :return: The relations in schema
        :retype: List[self.Relation]
        """
        raise dbt.exceptions.NotImplementedException(
            '`list_relations_without_caching` is not implemented for this '
            'adapter!'
        )

    ###
    # Provided methods about relations
    ###
    @available
    def get_missing_columns(self, from_schema, from_table, to_schema, to_table,
                            model_name=None):
        """Returns dict of {column:type} for columns in from_table that are
        missing from to_table"""
        from_columns = {
            col.name: col for col in
            self.get_columns_in_table(
                from_schema, from_table,
                model_name=model_name)
        }
        to_columns = {
            col.name: col for col in
            self.get_columns_in_table(
                to_schema, to_table,
                model_name=model_name)
        }

        missing_columns = set(from_columns.keys()) - set(to_columns.keys())

        return [
            col for (col_name, col) in from_columns.items()
            if col_name in missing_columns
        ]

    @available
    def get_columns_in_table(self, schema_name, table_name, database=None,
                             model_name=None):
        relation = self.Relation.create(
            schema=schema_name,
            identifier=table_name,
            database=database,
            type='table',
            quote_policy=self.config.quoting
        )
        return self.get_columns_in_relation(relation, model_name=model_name)

    @available
    def expand_target_column_types(self, temp_table, to_schema, to_table,
                                   model_name=None):
        goal = self.Relation.create(
            schema=None,
            identifier=temp_table,
            type='table',
            quote_policy=self.config.quoting
        )
        current = self.Relation.create(
            schema=to_schema,
            identifier=to_table,
            type='table',
            quote_policy=self.config.quoting
        )
        self.expand_column_types(goal, current, model_name)

    def list_relations(self, schema, model_name=None):
        if self._schema_is_cached(schema, model_name):
            return self.cache.get_relations(schema)

        # we can't build the relations cache because we don't have a
        # manifest so we can't run any operations.
        relations = self.list_relations_without_caching(
            schema, model_name=model_name
        )

        logger.debug('with schema={}, model_name={}, relations={}'
                     .format(schema, model_name, relations))
        return relations

    @available
    def query_for_existing(self, schemas, model_name=None):
        if not isinstance(schemas, (list, tuple)):
            schemas = [schemas]

        all_relations = []

        for schema in schemas:
            all_relations.extend(self.list_relations(schema, model_name))

        return {relation.identifier: relation.type
                for relation in all_relations}

    def _make_match_kwargs(self, schema, identifier):
        quoting = self.config.quoting
        if identifier is not None and quoting['identifier'] is False:
            identifier = identifier.lower()

        if schema is not None and quoting['schema'] is False:
            schema = schema.lower()

        return filter_null_values({'identifier': identifier,
                                   'schema': schema})

    def _make_match(self, relations_list, schema, identifier):

        matches = []

        search = self._make_match_kwargs(schema, identifier)

        for relation in relations_list:
            if relation.matches(**search):
                matches.append(relation)

        return matches

    @available
    def get_relation(self, schema, identifier, model_name=None):
        relations_list = self.list_relations(schema, model_name)

        matches = self._make_match(relations_list, schema, identifier)

        if len(matches) > 1:
            dbt.exceptions.get_relation_returned_multiple_results(
                {'identifier': identifier, 'schema': schema}, matches)

        elif matches:
            return matches[0]

        return None

    ###
    # ODBC FUNCTIONS -- these should not need to change for every adapter,
    #                   although some adapters may override them
    ###
    @abc.abstractmethod
    @available
    def create_schema(self, schema, model_name=None):
        """Create the given schema if it does not exist.

        :param str schema: The schema name to create.
        :param Optional[str] model_name: The name of the model to use for the
            connection.
        """
        raise dbt.exceptions.NotImplementedException(
            '`create_schema` is not implemented for this adapter!'
        )

    @abc.abstractmethod
    def drop_schema(self, schema, model_name=None):
        """Drop the given schema (and everything in it) if it exists.

        :param str schema: The schema name to drop.
        :param Optional[str] model_name: The name of the model to use for the
            connection.
        """
        raise dbt.exceptions.NotImplementedException(
            '`drop_schema` is not implemented for this adapter!'
        )

    @available
    def already_exists(self, schema, table, model_name=None):
        relation = self.get_relation(schema=schema, identifier=table)
        return relation is not None

    @available_raw
    @abstractclassmethod
    def quote(cls, identifier):
        """Quote the given identifier, as appropriate for the database.

        :param str identifier: The identifier to quote
        :return: The quoted identifier
        :rtype: str
        """
        raise dbt.exceptions.NotImplementedException(
            '`quote` is not implemented for this adapter!'
        )

    @available
    def quote_as_configured(self, identifier, quote_key, model_name=None):
        """Quote or do not quote the given identifer as configured in the
        project config for the quote key.

        The quote key should be one of 'database' (on bigquery, 'profile'),
        'identifier', or 'schema', or it will be treated as if you set `True`.
        """
        default = self.Relation.DEFAULTS['quote_policy'].get(quote_key)
        if self.config.quoting.get(quote_key, default):
            return self.quote(identifier)
        else:
            return identifier

    ###
    # Conversions: These must be implemented by concrete implementations, for
    # converting agate types into their sql equivalents.
    ###
    @abstractclassmethod
    def convert_text_type(cls, agate_table, col_idx):
        """Return the type in the database that best maps to the agate.Text
        type for the given agate table and column index.

        :param agate.Table agate_table: The table
        :param int col_idx: The index into the agate table for the column.
        :return: The name of the type in the database
        :rtype: str
        """
        raise dbt.exceptions.NotImplementedException(
            '`convert_text_type` is not implemented for this adapter!')

    @abstractclassmethod
    def convert_number_type(cls, agate_table, col_idx):
        """Return the type in the database that best maps to the agate.Number
        type for the given agate table and column index.

        :param agate.Table agate_table: The table
        :param int col_idx: The index into the agate table for the column.
        :return: The name of the type in the database
        :rtype: str
        """
        raise dbt.exceptions.NotImplementedException(
            '`convert_number_type` is not implemented for this adapter!')

    @abstractclassmethod
    def convert_boolean_type(cls, agate_table, col_idx):
        """Return the type in the database that best maps to the agate.Boolean
        type for the given agate table and column index.

        :param agate.Table agate_table: The table
        :param int col_idx: The index into the agate table for the column.
        :return: The name of the type in the database
        :rtype: str
        """
        raise dbt.exceptions.NotImplementedException(
            '`convert_boolean_type` is not implemented for this adapter!')

    @abstractclassmethod
    def convert_datetime_type(cls, agate_table, col_idx):
        """Return the type in the database that best maps to the agate.DateTime
        type for the given agate table and column index.

        :param agate.Table agate_table: The table
        :param int col_idx: The index into the agate table for the column.
        :return: The name of the type in the database
        :rtype: str
        """
        raise dbt.exceptions.NotImplementedException(
            '`convert_datetime_type` is not implemented for this adapter!')

    @abstractclassmethod
    def convert_date_type(cls, agate_table, col_idx):
        """Return the type in the database that best maps to the agate.Date
        type for the given agate table and column index.

        :param agate.Table agate_table: The table
        :param int col_idx: The index into the agate table for the column.
        :return: The name of the type in the database
        :rtype: str
        """
        raise dbt.exceptions.NotImplementedException(
            '`convert_date_type` is not implemented for this adapter!')

    @abstractclassmethod
    def convert_time_type(cls, agate_table, col_idx):
        """Return the type in the database that best maps to the
        agate.TimeDelta type for the given agate table and column index.

        :param agate.Table agate_table: The table
        :param int col_idx: The index into the agate table for the column.
        :return: The name of the type in the database
        :rtype: str
        """
        raise dbt.exceptions.NotImplementedException(
            '`convert_time_type` is not implemented for this adapter!')

    @available_raw
    @classmethod
    def convert_type(cls, agate_table, col_idx):
        return cls.convert_agate_type(agate_table, col_idx)

    @classmethod
    def convert_agate_type(cls, agate_table, col_idx):
        agate_type = agate_table.column_types[col_idx]
        conversions = [
            (agate.Text, cls.convert_text_type),
            (agate.Number, cls.convert_number_type),
            (agate.Boolean, cls.convert_boolean_type),
            (agate.DateTime, cls.convert_datetime_type),
            (agate.Date, cls.convert_date_type),
            (agate.TimeDelta, cls.convert_time_type),
        ]
        for agate_cls, func in conversions:
            if isinstance(agate_type, agate_cls):
                return func(agate_table, col_idx)

    ###
    # Operations involving the manifest
    ###
    def run_operation(self, manifest, operation_name):
        """Look the operation identified by operation_name up in the manifest
        and run it.

        Return an an AttrDict with three attributes: 'table', 'data', and
            'status'. 'table' is an agate.Table.
        """
        operation = manifest.find_operation_by_name(operation_name, 'dbt')

        # This causes a reference cycle, as dbt.context.runtime.generate()
        # ends up calling get_adapter, so the import has to be here.
        import dbt.context.runtime
        context = dbt.context.runtime.generate(
            operation,
            self.config,
            manifest,
        )

        result = operation.generator(context)()
        return result

    @classmethod
    def _catalog_filter_table(cls, table, manifest):
        """Filter the table as appropriate for catalog entries. Subclasses can
        override this to change filtering rules on a per-adapter basis.
        """
        return table.where(_catalog_filter_schemas(manifest))

    def get_catalog(self, manifest):
        """Get the catalog for this manifest by running the get catalog
        operation. Returns an agate.Table of catalog information.
        """
        try:
            table = self.run_operation(manifest, GET_CATALOG_OPERATION_NAME)
        finally:
            self.release_connection(GET_CATALOG_OPERATION_NAME)

        results = self._catalog_filter_table(table, manifest)
        return results

    def cancel_open_connections(self):
        """Cancel all open connections."""
        return self.connections.cancel_open()
