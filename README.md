# VolumeWitness

See if your next Docker Compose deploy will switch database volumes.

A directory move, project-name change, or Compose override can change the
resolved volume name while the service still uses the same volume key. Compare
the current container's mount with the next Compose project's resolved mount
before deployment.

```text
CHANGED   "db" at "/var/lib/postgresql/data"
  Current:  "demo_pgdata"
  Proposed: "demo_v2_pgdata"
  the resolved Docker volume names differ.
```

## Try it

Python 3.10 or newer. The script uses only the standard library.

Run the included synthetic example without Docker from the repository root.

```sh
python volumewitness.py --service db --target /var/lib/postgresql/data --mounts-json examples/mounts.json --model-json examples/next.json
```

For a live check, install Docker CLI with the Compose plugin and select the
engine that hosts the current container.

```sh
python volumewitness.py --container demo-db-1 --service db --target /var/lib/postgresql/data -f compose.yaml -p demo_v2
```

Use the same working directory, environment, Docker context, Compose files,
profiles, and project name that the intended deployment will use. Repeat
`-f` for overrides in order. `--context`, `--env-file`, `--profile`,
and `--project-directory` are supported. The service name is from the
**proposed** Compose model, and the target is the exact mount path inside
the container.

## Exit codes

| Code | Status | Meaning |
| --- | --- | --- |
| 0 | `same` | Both inputs resolve to the same Docker volume name. |
| 1 | `changed` | The volume name changed, or the target switches between a Docker volume and another mount type. |
| 2 | `unknown` | There is no explicit comparable mount, such as a missing service, an anonymous volume, or two bind mounts. |
| 2 | `error` | A command or input cannot be read or parsed. |

Add `--format json` for machine-readable output. The check runs
`docker container inspect` and `docker compose config`. It does not read
database data, start containers, or modify Docker volumes.

The result compares **mount type and resolved volume name** on one Docker
engine. It does not verify volume contents, mount mode, driver options, or
whether Compose may recreate a volume under the same name. It also cannot
predict which data directory a new image or application configuration will
actually use.

## Offline inputs

Use the `.Mounts` array from `docker container inspect` and the normalized
JSON from `docker compose config --format json` with `--mounts-json` and
`--model-json`. Collect both on the same intended Docker engine. Rendered
Compose files can contain expanded environment values, so keep real snapshots
local.

## Tests

```sh
python -m unittest discover -s tests -v
```

The suite exercises comparison rules, CLI exit codes, and the two read-only
Docker commands. When Docker Compose is installed, it also renders real
Compose models to check naming and overrides without starting an engine.
