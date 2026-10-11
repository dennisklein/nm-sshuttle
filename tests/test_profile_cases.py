# SPDX-License-Identifier: MIT
"""The cases shared with the GNOME editor's validator (gnome/tests/test-validate.c)."""

import copy
import json
import pathlib

import pytest

from fakes import fake_getpwnam
from nm_sshuttle.profile import ProfileError, parse

SPEC = json.loads((pathlib.Path(__file__).parent / "profile-cases.json").read_text())


def build(case):
    c = copy.deepcopy(SPEC["base"])
    data = c["data"]
    for key, value in case.get("data", {}).items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    for key in ("permissions", "persistent", "ipv4_method"):
        c[key] = case.get(key, c[key])
    ipv4 = {} if c["ipv4_method"] is None else {"method": c["ipv4_method"]}
    return {"connection": {"id": "corp", "uuid": "u-1", "permissions": c["permissions"]},
            "vpn": {"data": data, "persistent": c["persistent"]}, "ipv4": ipv4}


@pytest.mark.parametrize("case", SPEC["cases"], ids=lambda c: c["name"])
def test_case(case):
    settings = build(case)
    if "error" not in case:
        parse(settings, getpwnam=fake_getpwnam)
        return
    with pytest.raises(ProfileError) as err:
        parse(settings, getpwnam=fake_getpwnam)
    assert str(err.value).startswith(case["error"])
