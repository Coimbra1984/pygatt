import os

__title__ = 'pygattpi'
if "GITHUB_REF" in os.environ:
    __version__ = os.environ["GITHUB_REF"].split("/")[-1]
else:
    __version__ = "0.0.0"
__license__ = 'Apache License, Version 2.0 and MIT License'
__copyright__ = 'Copyright 2015 Stratos Inc. and Orion Labs'

try:
    from setuptools import setup, find_packages
except ImportError:
    from distutils.core import setup  # pylint: disable=F0401,E0611

with open('README.rst') as f:
    readme = f.read()
with open('CHANGELOG.rst') as f:
    changelog = f.read()

setup(
    name=__title__,
    version=__version__,
    description='Python Bluetooth LE (Low Energy) and GATT Library, forked from peplin/pygatt',
    author='Markus Proeller <markus.proeller@pieye.org>',
    author_email='markus.proeller@pieye.org',
    packages=find_packages(exclude=("tests", "tests.*")),
    package_data={'': ['LICENSE']},
    license="Apache 2.0 and MIT",
    long_description=readme + '\n\n' + changelog,
    url='https://github.com/lemarquois/pygatt',
    install_requires=[
        'pyserial',
        'enum-compat'
    ],
    setup_requires=[
        'coverage >= 3.7.1',
        'nose >= 1.3.7'
    ],
    extras_require={
        'GATTTOOL': ["pexpect"],
    },
    package_dir={'pygattpi': 'pygattpi'},
    zip_safe=False,
    include_package_data=True,
    classifiers=(
        'Intended Audience :: Developers',
        'License :: OSI Approved :: Apache Software License',
        'License :: OSI Approved :: MIT License',
        'Programming Language :: Python',
        'Programming Language :: Python :: 2.7',
        'Programming Language :: Python :: 3',
    )
)
