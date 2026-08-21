"""The published contract, printed so it can be committed beside the code.

The frontend's types are generated from the committed copy rather than from a
running backend (#35): CI's frontend job has no Python in it, and a document
that exists only while a developer has the server up is one that drifts in
silence. `test_openapi_document` is what keeps the copy honest — including at a release,
since the document carries the package version and a version bump is a change to
it like any other.
"""

import json
import os
import sys
from typing import Any

from cryptography.fernet import Fernet

# `ludarium.main` builds the module-level app as it is imported, so the
# environment has to be complete before the import rather than before the call
# below. The values are placeholders and none of them is read: nothing in the
# document depends on a setting, and a schema printable only on a configured
# machine would be regenerated less often than it changes. The Fernet key is
# generated rather than written down, so this file carries nothing key-shaped.
os.environ.setdefault("LUDARIUM_SECRET_KEY", "not-a-secret")
os.environ.setdefault("LUDARIUM_PASSWORD", "not-a-password")
os.environ.setdefault("LUDARIUM_ENCRYPTION_KEY", Fernet.generate_key().decode())

from ludarium.main import create_app


def document() -> dict[str, Any]:
    return create_app().openapi()


def main() -> None:
    """To stdout, so the caller chooses the path.

    The package is installed into a container that has no `docs/`, and a
    hardcoded repository path would be wrong everywhere it actually runs.
    """

    # Sorted and indented because this file is read as a diff far more often
    # than it is read as JSON: unsorted, a schema added in the middle would
    # rewrite every line after it.
    json.dump(document(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
