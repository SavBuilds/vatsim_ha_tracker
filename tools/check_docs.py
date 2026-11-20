#!/usr/bin/env python3
"""Check that every name the README mentions exists in the release tree.

    python tools/check_docs.py

Checks, from the repo root:
  - every vatsim entity id in README.md is published by the app or defined in a package
  - every attribute named in the "Entity reference" section is a string the app publishes
  - every config key in the README and in apps.yaml.example is read by the app, and
    every key the app reads is in apps.yaml.example
  - every state value listed in the entity table is a string in the app
Exit status 0 when clean, 1 otherwise.
"""
import os
import re
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def main():
    readme = read("README.md")
    app = read("apps", "vatsim_atc_tracker", "vatsim_atc_tracker.py")
    example = read("apps", "vatsim_atc_tracker", "apps.yaml.example")
    packages = read("packages", "vatsim.yaml") + read("packages", "vatsim_user.example.yaml")
    problems = []

    strings = set(re.findall(r'"([A-Za-z0-9_./ -]+)"', app))
    published = {s for s in strings if re.fullmatch(r"(sensor|binary_sensor|device_tracker)\.vatsim_[a-z_]+", s)}
    helper = set()
    for domain in ("input_boolean", "input_text", "input_select", "input_datetime", "script"):
        block = re.search(rf"^{domain}:\n((?:[ #].*\n|\n)+)", packages, re.M)
        for chunk in re.findall(rf"^{domain}:\n((?:[ #].*\n|\n)+)", packages, re.M):
            helper |= {f"{domain}.{k}" for k in re.findall(r"^  ([a-z0-9_]+):", chunk, re.M)}
    helper |= {"binary_sensor." + u for u in re.findall(r"unique_id: ([a-z0-9_]+)", packages)}

    entity_re = re.compile(r"\b((?:sensor|binary_sensor|device_tracker|input_boolean|input_text|input_select|"
                           r"input_datetime|script)\.vatsim_[a-z0-9_]+)")
    for entity in sorted(set(entity_re.findall(readme))):
        if entity not in published and entity not in helper:
            problems.append(f"README entity not in app or packages: {entity}")
    for entity in sorted(published):
        if entity not in readme:
            problems.append(f"app entity missing from README: {entity}")

    section = readme.split("## Entity reference", 1)[1].split("\n## ", 1)[0]
    non_names = {"on", "off", "true", "false", "none", "int", "string", "lower", "is", "points"}
    for token in sorted(set(re.findall(r"`([a-z][a-z0-9_]*)`", section))):
        if token not in strings and token not in non_names and token not in {"friendly_name"}:
            problems.append(f"README entity-reference name not published by the app: {token}")
    for token in sorted(set(re.findall(r"`([A-Z]{3})`", section))):
        if token not in strings:
            problems.append(f"README value not in the app: {token}")

    read_keys = set(re.findall(r'(?:num|_cfg_optional_str)\(\s*"([a-z_]+)"', app)) | {"vatsim_cid"}
    example_keys = set(re.findall(r"^\s*#?\s*([a-z_]+):", example, re.M)) - {"module", "class", "vatsim_atc_tracker"}
    for key in sorted(read_keys - example_keys):
        problems.append(f"config key read by the app but not in apps.yaml.example: {key}")
    for key in sorted(example_keys - read_keys):
        problems.append(f"apps.yaml.example key the app does not read: {key}")
    config_like = set(re.findall(r"`([a-z]+(?:_[a-z]+)+)`", readme))
    suffixes = ("_path", "_url", "_seconds", "_minutes", "_days", "_knots", "_nm", "_ft", "_feet", "_polls", "_deg",
                "_misses", "_multiplier", "_points", "_cid")
    for key in sorted(k for k in config_like if k.endswith(suffixes) and k not in strings and k not in read_keys):
        problems.append(f"README names a config key or attribute that does not exist: {key}")
    for key in re.findall(r"^  ([a-z_]+):", readme.split("```yaml\nvatsim_atc_tracker:", 1)[1].split("```", 1)[0], re.M):
        if key not in read_keys | {"module", "class"}:
            problems.append(f"README apps.yaml example uses an unknown key: {key}")

    for line in problems:
        print(line)
    print(f"{len(problems)} problem(s). Checked {len(set(entity_re.findall(readme)))} entity ids, "
          f"{len(read_keys)} config keys, {len(published)} published entities.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
