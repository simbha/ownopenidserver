#!/usr/bin/env python

from distutils.core import setup

setup(
        name='openidserver',
        author='Vladimir S Eremin (aka yottatsa)',
        author_email='me@yottatsa.name',
        url='http://ownopenidserver.com/',
        version='0.11',
        packages=['openidserver'],
        package_data = {
            'openidserver': [
                        'templates/base.html',
                        'templates/login.html',
                        'templates/verify.html',
                        'templates/password.html',
                        'templates/trusted.html',
                        'templates/trusted_confirm.html',
                    ],
            },
)
