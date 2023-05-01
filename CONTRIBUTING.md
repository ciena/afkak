# Contributing to Afkak

We welcome [pull requests](https://help.github.com/articles/about-pull-requests/) from the community.
This document describes the associated standards.

## Code Standards

* Please lint your code via `tox -e py38-lint` before submission.
  * This checks for compliance with PEP 8 and limits line length to 120 characters.
* Please also format your code via `make fmt` before you commit changes.
  * this will format any changes to the pre-configured `black` and `isort` definitions.
  * the pr tests `lint` will validate this has been completed
* All tests must pass.
* If you introduce new functionality, update the documentation accordingly.
* Add yourself to [AUTHORS.md](./AUTHORS.md).

## Signing Your Commits

Before we can merge your contribution you must sign your commits to indicate acceptance of the [Developer Certificate of Origin](http://developercertificate.org/) (DCO), reproduced below:

    Developer Certificate of Origin
    Version 1.1

    Copyright (C) 2004, 2006 The Linux Foundation and its contributors.
    1 Letterman Drive
    Suite D4700
    San Francisco, CA, 94129

    Everyone is permitted to copy and distribute verbatim copies of this
    license document, but changing it is not allowed.


    Developer's Certificate of Origin 1.1

    By making a contribution to this project, I certify that:

    (a) The contribution was created in whole or in part by me and I
        have the right to submit it under the open source license
        indicated in the file; or

    (b) The contribution is based upon previous work that, to the best
        of my knowledge, is covered under an appropriate open source
        license and I have the right under that license to submit that
        work with modifications, whether created in whole or in part
        by me, under the same open source license (unless I am
        permitted to submit under a different license), as indicated
        in the file; or

    (c) The contribution was provided directly to me by some other
        person who certified (a), (b) or (c) and I have not modified
        it.

    (d) I understand and agree that this project and the contribution
        are public and that a record of the contribution (including all
        personal information I submit with it, including my sign-off) is
        maintained indefinitely and may be redistributed consistent with
        this project or the open source license(s) involved.

The license in question is the [Apache License, version 2.0](./LICENSE), as distributed in the repository root.
To indicate acceptance, include a line like this in each commit in your pull request:

    Signed-off-by: Your Name <your.email@domain.example>

You must use your real name.
We cannot accept anonymous or pseudonymous contributions.

The easiest way to add this line is to use the `-s` or `--signoff` flags to the `git commit` command.
If you didn’t use that flag when committing then the easiest way to add it is to `git rebase -i` and use the `reword` action on each commit.
