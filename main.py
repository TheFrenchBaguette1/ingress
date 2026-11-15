import config
from sequence import SequenceRequest, build_sequence


def main():
    request = SequenceRequest(**config.DEFAULT_REQUEST)
    manifest = build_sequence(request)

    print(f"Status: {manifest['status']}")
    print(f"Frames: {manifest['frame_count']}")
    print(f"Can animate: {manifest['can_animate']}")
    print(f"Failures: {len(manifest['failures'])}")


if __name__ == "__main__":
    main()