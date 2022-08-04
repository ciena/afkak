# -*- coding: utf-8 -*-

import afkak

extensions = [
    'sphinx.ext.autodoc',
    'sphinx.ext.napoleon',
    'sphinx.ext.doctest',
    'sphinx.ext.intersphinx',
    'sphinx.ext.coverage',
    'sphinx.ext.viewcode',
]

templates_path = ['_templates']
source_suffix = '.rst'
master_doc = 'index'

# General information about the project.
project = u'Afkak'
copyright = u'2015–2019 Ciena Corporation'
author = u'Robert Thille'

# The version info for the project you're documenting, acts as replacement for
# |version| and |release|, also used in various other places throughout the
# built documents.
#
# The short X.Y version.
version = afkak.__version__
# The full version, including alpha/beta/rc tags.
release = version

language = None
exclude_patterns = ['_build']

pygments_style = 'sphinx'

napoleon_use_ivar = True
napoleon_use_param = True
napoleon_use_rtype = True

html_theme = 'alabaster'
html_logo = '_static/afkak.png'
html_static_path = ['_static']
html_extra_path = ['.nojekyll']  # Disable GitHub Pages' processing.

# The _cache directory is opportunistically updated by the script
# tools/download-intersphinx, which reads this data structure for the URLs and
# filenames to write.
intersphinx_mapping = {
    'python': ('https://docs.python.org/3/', '_cache/python.inv'),
    'twisted': ('https://docs.twistedmatrix.com/en/stable/api/', '_cache/twisted.inv'),
}
