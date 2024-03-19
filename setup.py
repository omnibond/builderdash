import setuptools

setuptools.setup(
    name="builderdash",
    version="0.0.1",
    license="LGPL",
    author="Omnibond Systems LLC",
    author_email="support@cloudycluster.com",
    description="Builderdash",
    long_description="",
    long_description_content_type="text/plain",
    url="https://www.cloudycluster.com/",
    packages=setuptools.find_packages(),
    python_requires='>=3.6',
    install_requires=[
        'botocore',
        'google-api-python-client',
        'paramiko',
        'requests',
        'pyyaml'
    ],
    entry_points={
        "console_scripts": ["builderdash = builderdash.main:main"]
    }
)
