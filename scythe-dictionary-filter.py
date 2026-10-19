#!/usr/bin/env python3
"""Remove telemetry channels that carry both "packed_channel" and "offset" fields.

These are ground-derived (bit-packed) channels that don't exist in flight software.
"""
import argparse
import json
import sys


def is_packed(channel) -> bool:
    return "packed_channel" in channel and "offset" in channel


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="Input F Prime JSON dictionary")
    parser.add_argument("output", help="Output JSON dictionary")
    args = parser.parse_args()

    with open(args.input) as f:
        dictionary = json.load(f)

    channels = dictionary["telemetryChannels"]
    kept = [c for c in channels if not is_packed(c)]
    print(f"telemetryChannels: removed {len(channels) - len(kept)} of {len(channels)}", file=sys.stderr)
    dictionary["telemetryChannels"] = kept

    with open(args.output, "w") as f:
        json.dump(dictionary, f, indent=2)
        f.write("\n")


if __name__ == "__main__":
    main()
