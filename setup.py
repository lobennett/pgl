import numpy
from setuptools import setup, find_packages, Extension
import sys
from pathlib import Path

if sys.platform != 'darwin':
    raise RuntimeError("This package only works on macOS")

displayInfoExtension = Extension(
    'pgl._resolution', 
    sources=['pgl/_resolution.m'],
    extra_compile_args=['-ObjC'],
    extra_link_args=[
        '-framework', 'CoreGraphics',
        '-framework', 'Cocoa',
        '-framework', 'CoreFoundation'
    ]
)

gammaTableExtension = Extension(
    'pgl._pglGammaTable',
    sources=['pgl/_pglGammaTable.m'],
    include_dirs=[numpy.get_include()], 
    extra_compile_args=['-ObjC'],
    extra_link_args=[
        '-framework', 'CoreGraphics',
        '-framework', 'Cocoa',
        '-framework', 'CoreFoundation'
    ]
)

timestampExtension = Extension(
    'pgl._pglTimestamp',
    sources=['pgl/_pglTimestamp.c'],  
    extra_compile_args=[],
    extra_link_args=[]
)

eventListenerExtension = Extension(
    'pgl._pglEventListener',
    sources=['pgl/_pglEventListener.cpp'],
    extra_link_args=[
        '-framework', 'ApplicationServices',
        '-framework', 'Carbon'
    ]
)

metalPackageData = [
    str(path.relative_to("metal"))
    for path in Path("metal").rglob("*")
    if path.is_file() and path.name != "__init__.py"
]

setup(
    name='pgl',  
    version='0.1.0',
    packages=find_packages(),
    package_data={"metal": metalPackageData},
    description='PGL Psychophysics and experiment library',
    python_requires='>=3.9',
    ext_modules=[displayInfoExtension,gammaTableExtension,timestampExtension,eventListenerExtension]
)
