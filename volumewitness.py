"""Compare a container's mounted volume with the next Docker Compose model."""

import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import subprocess
import sys


VERSION = "0.1.0"
EXIT_CODES = {"same": 0, "changed": 1, "unknown": 2, "error": 2}


class InputError(Exception):
    """The inputs could not be read or were not valid."""


@dataclass(frozen=True)
class Finding:
    status: str
    reason: str
    current_type: str | None = None
    current_volume: str | None = None
    proposed_type: str | None = None
    proposed_volume: str | None = None


def find_mount(entries, destination_key, target, source):
    if not isinstance(entries, list):
        raise InputError(f"{source} mounts must be an array")
    matches = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get(destination_key), str):
            raise InputError(f"{source} mounts must have a destination string")
        if entry[destination_key] == target:
            matches.append(entry)
    if len(matches) > 1:
        raise InputError(f"{source} mounts contain the destination more than once")
    return next(iter(matches), None)


def mount_type(mount):
    value = mount.get("Type", mount.get("type"))
    if not isinstance(value, str) or not value:
        raise InputError("a mount is missing its type")
    return value


def compare(mounts, model, service, target):
    if not isinstance(model, dict) or not isinstance(model.get("services"), dict):
        raise InputError("expected a rendered Compose JSON object with a services map")
    current = find_mount(mounts, "Destination", target, "current")
    if current is None:
        return Finding("unknown", "the current container has no mount at this target")

    current_type = mount_type(current)
    current_name = current.get("Name") if current_type == "volume" else None
    if current_type == "volume" and (not isinstance(current_name, str) or not current_name):
        raise InputError("the current Docker volume mount is missing its name")

    proposed_service = model["services"].get(service)
    if proposed_service is None:
        return Finding("unknown", "the service is absent from the rendered Compose model",
                       current_type, current_name)
    if not isinstance(proposed_service, dict):
        raise InputError("the rendered Compose service must be an object")
    proposed = find_mount(proposed_service.get("volumes", []), "target", target, "proposed")
    if proposed is None:
        return Finding("unknown", "the proposed service has no explicit mount at this target",
                       current_type, current_name)

    proposed_type = mount_type(proposed)
    volume_key = proposed.get("source")
    if proposed_type == "volume" and volume_key:
        volumes = model.get("volumes")
        if not isinstance(volumes, dict) or not isinstance(volume_key, str):
            raise InputError("the proposed named volume has no valid top-level definition")
        volume = volumes.get(volume_key)
        if not isinstance(volume, dict) or not isinstance(volume.get("name"), str) or not volume["name"]:
            raise InputError("the proposed volume is missing its resolved name; use docker compose config")
        proposed_name = volume["name"]
    else:
        proposed_name = None

    details = (current_type, current_name, proposed_type, proposed_name)
    if current_type != proposed_type and "volume" in (current_type, proposed_type):
        return Finding("changed", "the mount switches between a Docker volume and another mount type", *details)
    if proposed_type != "volume":
        return Finding("unknown", "only Docker volume names are compared", *details)
    if proposed_name is None:
        return Finding("unknown", "the proposed volume is anonymous", *details)
    if proposed_name != current_name:
        return Finding("changed", "the resolved Docker volume names differ", *details)
    return Finding("same", "the resolved Docker volume names match", *details)


def read_json(path, description):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError) as error:
        raise InputError(f"could not read {description} as JSON") from error


def docker_json(args, context, description):
    command = ["docker"]
    if context is not None:
        command.append(f"--context={context}")
    command.extend(args)
    try:
        result = subprocess.run(command, capture_output=True, check=True, timeout=30)
    except FileNotFoundError as error:
        raise InputError("Docker CLI was not found on PATH") from error
    except subprocess.TimeoutExpired as error:
        raise InputError(f"Docker {description} timed out") from error
    except subprocess.CalledProcessError as error:
        raise InputError(f"Docker {description} failed; check the equivalent Docker command directly") from error
    except OSError as error:
        raise InputError(f"could not run Docker {description}") from error
    try:
        return json.loads(result.stdout)
    except (ValueError, UnicodeError) as error:
        raise InputError(f"Docker {description} did not return valid JSON") from error


def compose_config_args(args):
    command = ["compose"]
    for name, values in (
        ("file", args.file),
        ("env-file", args.env_file),
        ("profile", args.profile),
    ):
        command.extend(f"--{name}={value}" for value in values)
    for name, value in (
        ("project-name", args.project_name),
        ("project-directory", args.project_directory),
    ):
        if value is not None:
            command.append(f"--{name}={value}")
    return [*command, "config", "--format", "json"]


def load_inputs(args):
    if args.mounts_json is not None:
        return read_json(args.mounts_json, "mounts"), read_json(args.model_json, "Compose model")
    mounts = docker_json(
        ["container", "inspect", "--format", "{{json .Mounts}}", "--", args.container],
        args.context,
        "container inspect",
    )
    model = docker_json(compose_config_args(args), args.context, "compose config")
    return mounts, model


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"VolumeWitness {VERSION}")
    parser.add_argument("--service", required=True, help="service name in the proposed Compose project")
    parser.add_argument("--target", required=True, help="exact mount destination inside the container")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--container", help="name or ID of the current container")
    inputs.add_argument("--mounts-json", type=Path, help="saved Docker .Mounts array")
    parser.add_argument("--model-json", type=Path, help="saved output of docker compose config --format json")
    parser.add_argument("-f", "--file", action="append", default=[], help="Compose file, repeat for overrides")
    parser.add_argument("-p", "--project-name", help="proposed Compose project name")
    parser.add_argument("--env-file", action="append", default=[], help="Compose interpolation env file")
    parser.add_argument("--profile", action="append", default=[], help="enable a Compose profile")
    parser.add_argument("--project-directory", help="Compose project's working directory")
    parser.add_argument("--context", help="Docker context for both commands")
    args = parser.parse_args(argv)
    if not args.service or not args.target or args.container == "":
        parser.error("service, target, and container must not be empty")
    if args.mounts_json is not None:
        if args.model_json is None:
            parser.error("--mounts-json requires --model-json")
        if any((args.file, args.project_name, args.env_file, args.profile, args.project_directory, args.context)):
            parser.error("Compose and Docker flags apply only with --container")
    elif args.model_json is not None:
        parser.error("--model-json requires --mounts-json")
    return args


def describe_mount(kind, name):
    if kind is None:
        return "(unavailable)"
    if name is not None:
        return json.dumps(name)
    return f"({json.dumps(kind)} without a resolved Docker volume name)"


def main(argv=None):
    args = parse_args(argv)
    try:
        mounts, model = load_inputs(args)
        finding = compare(mounts, model, args.service, args.target)
    except InputError as error:
        finding = Finding("error", str(error))

    if args.format == "json":
        print(json.dumps({"service": args.service, "target": args.target, **asdict(finding)}, indent=2))
    elif finding.status == "error":
        print(f"ERROR   {finding.reason}", file=sys.stderr)
    else:
        print(f"{finding.status.upper()}   {json.dumps(args.service)} at {json.dumps(args.target)}")
        print(f"  Current:  {describe_mount(finding.current_type, finding.current_volume)}")
        print(f"  Proposed: {describe_mount(finding.proposed_type, finding.proposed_volume)}")
        print(f"  {finding.reason}.")
    return EXIT_CODES[finding.status]


if __name__ == "__main__":
    raise SystemExit(main())
