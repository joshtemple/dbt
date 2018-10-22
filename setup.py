#!/usr/bin/env python
from setuptools import find_packages
from distutils.core import setup
import os

def read(fname):
    return open(os.path.join(os.path.dirname(__file__), fname)).read()

package_name = "dbt"
package_version = "0.11.1"
description = """dbt (data build tool) is a command line tool that helps \
analysts and engineers transform data in their warehouse more effectively"""


boto_requirements = [
    'boto3>=1.6.23,<1.8.0',
    'botocore>=1.9.23,<1.11.0',
]

postgres_requirements = [
    'psycopg2>=2.7.5,<2.8',
]


setup(
    name=package_name,
    version=package_version,
    description=description,
    long_description_content_type=description,
    author="Fishtown Analytics",
    author_email="info@fishtownanalytics.com",
    url="https://github.com/fishtown-analytics/dbt",
    packages=find_packages(),
    package_data={
        'dbt': [
            'include/index.html',
            'include/global_project/dbt_project.yml',
            'include/global_project/docs/*.md',
            'include/global_project/macros/*.sql',
            'include/global_project/macros/**/*.sql',
            'include/global_project/macros/**/**/*.sql',
        ]
    },
    test_suite='test',
    entry_points={
        'console_scripts': [
            'dbt = dbt.main:main',
        ],
    },
    scripts=[
        'scripts/dbt',
    ],
    extras_require={
        'snowflake': ['snowflake-connector-python>=1.4.9'] + boto_requirements,
        'bigquery': ['google-cloud-bigquery>=1.0.0,<2'],
        'redshift': boto_requirements[:] + postgres_requirements[:],
        'postgres': postgres_requirements[:],
    },
    install_requires=[
        'Jinja2>=2.10',
        'PyYAML>=3.11',
        'sqlparse==0.2.3',
        'networkx==1.11',
        'minimal-snowplow-tracker==0.0.2',
        'requests>=2.18.0,<3',
        'colorama==0.3.9',
        'agate>=1.6,<2',
        'jsonschema==2.6.0',
    ]
)
