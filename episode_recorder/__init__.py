"""episode_recorder -- paired video and control capture, segmented into episodes.

Importing the package puts the pinned submodules on sys.path, so a consumer writes
`import episode_recorder` and `vcap` and `gpb_client` resolve. bootstrap.py is the only
module that knows where they live.
"""
from . import bootstrap as _bootstrap

_bootstrap.install()

__version__ = "0.1.0"
