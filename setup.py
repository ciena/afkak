# -*- coding: utf-8 -*-
# Copyright (C) 2016 Cyan, Inc.
# Copyright (C) 2016-2019 Ciena Corporation

from setuptools import find_packages, setup

# NB: This version is extracted by the Makefile using awk; don't change the
# formatting here!
version = "3.0.0"

with open('README.md', 'r') as fin:
    readme_lines = fin.readlines()
long_description = ''.join(readme_lines[
    readme_lines.index('<!-- LONG_DESCRIPTION_START -->\n') + 1:
    readme_lines.index('<!-- LONG_DESCRIPTION_END -->\n'):
])

setup(
    name="afkak",
    version=version,
    install_requires=[
        'six',
        'Twisted>=18.9.0',  # First release with twisted.logger.eventAsText
    ],
    # Afkak requires both b'' and u'' syntax, so it isn't compatible with early
    # Python 3 releases. Additionally, Python 3.3 is not supported because
    # nobody uses it, though we don't forbid install on that version.
    python_requires='>=2.7, !=3.0.*, !=3.1.*, !=3.2.*, <4',
    extras_require={
        'FastMurmur2': ['pyhash'],
        'snappy': ['python-snappy>=0.5'],
    },

    packages=find_packages(),
    include_package_data=True,

    zip_safe=True,

    author="Robert Thille",
    author_email="rthille@ciena.com",
    maintainer="Tom Most",
    maintainer_email="twm@freecog.net",
    url="https://github.com/ciena/afkak",
    project_urls={
        'Documentation': 'https://afkak.readthedocs.io/en/latest/',
        'Source': 'https://github.com/ciena/afkak',
        'Issues': 'https://github.com/ciena/afkak/issues',
    },
    license="Apache License 2.0",
    classifiers=[
        'Development Status :: 4 - Beta',
        'Framework :: Twisted',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: Apache Software License',
        'Operating System :: MacOS :: MacOS X',
        'Operating System :: POSIX',
        'Programming Language :: Python :: 2.7',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.5',
        'Programming Language :: Python :: 3.6',
        'Programming Language :: Python :: 3.7',
        'Programming Language :: Python :: Implementation :: CPython',
        'Programming Language :: Python :: Implementation :: PyPy',
        'Topic :: Communications',
        'Topic :: System :: Distributed Computing',
    ],

    description="Twisted Python client for Apache Kafka",
    long_description=long_description,
    long_description_content_type='text/markdown',
    keywords=['Kafka client', 'distributed messaging', 'txkafka'],
)
