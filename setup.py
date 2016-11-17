
from setuptools import setup, find_packages


setup(
    name="afkak",
    version="2.3.0",

    install_requires=['Twisted>=16.5.0'],
    extras_require={
        'FastMurmur2': ['Murmur>=0.1.3'],
    },

    packages=find_packages(),

    zip_safe=True,

    author="Robert Thille",
    author_email="rthille@ciena.com",
    url="https://github.com/ciena/afkak",
    license="Apache License 2.0",
    classifiers=[
        'Development Status :: 4 - Beta',
        'Framework :: Twisted',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: Apache Software License',
        'Operating System :: MacOS :: MacOS X',
        'Operating System :: POSIX',
        'Programming Language :: Python :: 2.7',
        'Programming Language :: Python :: Implementation :: CPython',
        'Programming Language :: Python :: Implementation :: PyPy',
        'Topic :: Communications',
        'Topic :: System :: Distributed Computing',
    ],

    description="Twisted Python client for Apache Kafka",
    long_description="""
This module provides low-level protocol support for Apache Kafka as well as
high-level consumer and producer classes. Request batching is supported by the
protocol as well as broker-aware request routing. Gzip and Snappy compression
is also supported for message sets.
""",
    keywords=['Kafka client', 'distributed messaging', 'txkafka']
)
