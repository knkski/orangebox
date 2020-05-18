Orange Box
==========

Introduction
------------

This repository hosts scripts useful for setting up an Orange Box.
To run these scripts, copy all files from this repository to the
Orange Box, then run:

    sudo python3 setup.py --ob-num=XX

Where `XX` is the Orange Box number that you are setting up. These
scripts expect to be run on an Ubuntu Focal installation that was
installed to the 120GB drive of the Orange Box.

There also exists setup.sh as a transliteration of bash scripts from
the `obinstall` repo. It is probably not up-to-date with setup.py.

Troubleshooting
---------------

The setup.py script is designed to be idempotent, which means you can
run it multiple times on the same system without breaking things. If
you run into any issues with the install process hanging or erroring
out, try running setup.py again.
