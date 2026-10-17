# Vendored py-uconnect

This is a copy of [py-uconnect](https://github.com/hass-uconnect/py-uconnect)
(Apache License 2.0, see `LICENSE`), taken from the `hass-fork` branch of
[815rx1/py-uconnect](https://github.com/815rx1/py-uconnect) at commit
`1e46eb4`, which is upstream 0.4.6 plus EMEA charge schedules
(hass-uconnect/py-uconnect#13), request timeouts, an authentication error and
a login lock.

It ships inside the integration so the fork does not depend on a PyPI release
of the library, and so it cannot clash with an installed `py-uconnect`. Changes
made here since then are recorded in the integration's git history.
