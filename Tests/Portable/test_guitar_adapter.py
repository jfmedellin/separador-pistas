import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from SeparationWorker.engine.publication import CancellationToken
from SeparationWorker.guitar_adapter import (
    LEAD_STEM,
    REGISTERED_SPECIALISTS,
    RHYTHM_STEM,
    ROLE_STEM_NAMES,
    GuitarSpecialistError,
    SpecialistEntry,
    resolve_specialist,
    separate_guitar_roles,
)

SPECIALIST_ID = "metal-lead-rhythm-v1"
ENTRYPOINT = "metal_specialist.separate"


def make_asset_root(
    root,
    *,
    model_file="metal/lead_rhythm.pt",
    model_bytes=b"weights",
    registered_bytes=None,
    network_required=False,
    create=True,
):
    asset_root = Path(root) / "assets"
    target = asset_root / model_file
    if create:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(model_bytes)
    digest = hashlib.sha256(registered_bytes if registered_bytes is not None else model_bytes).hexdigest()
    entry = SpecialistEntry(SPECIALIST_ID, ENTRYPOINT, model_file, digest, network_required)
    return asset_root, {SPECIALIST_ID: entry}


class FakeSpecialist:
    def __init__(self, *, fail=False, missing=(), output=""):
        self.fail = fail
        self.missing = set(missing)
        self.output = output
        self.commands = []

    def __call__(self, command):
        command = list(command)
        self.commands.append(command)
        if self.fail:
            raise subprocess.CalledProcessError(9, command, output=self.output)
        staging = Path(command[command.index("--out") + 1])
        device = command[command.index("--device") + 1]
        for name in ROLE_STEM_NAMES:
            if name not in self.missing:
                (staging / name).write_bytes(f"{device}:{name}".encode("ascii"))
        (staging / "unexpected.txt").write_text("not published", encoding="utf-8")


class ShippedRegistryTests(unittest.TestCase):
    def test_no_specialist_is_registered_in_the_shipped_build(self):
        self.assertEqual({}, dict(REGISTERED_SPECIALISTS))

    def test_shipped_registry_rejects_every_identifier(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(GuitarSpecialistError) as caught:
                resolve_specialist(SPECIALIST_ID, root)

        self.assertEqual("specialist.unregistered", caught.exception.code)
        self.assertIn("admit_metal_guitar_model.py", caught.exception.recovery)


class ResolveSpecialistTests(unittest.TestCase):
    def test_registered_specialist_resolves_to_verified_local_bytes(self):
        with tempfile.TemporaryDirectory() as root:
            asset_root, registry = make_asset_root(root)

            resolved = resolve_specialist(SPECIALIST_ID, asset_root, registry=registry)

            self.assertEqual(SPECIALIST_ID, resolved.specialist_id)
            self.assertEqual(ENTRYPOINT, resolved.entrypoint)
            self.assertEqual(hashlib.sha256(b"weights").hexdigest(), resolved.sha256)
            self.assertTrue(resolved.model_path.is_file())

    def test_empty_identifier_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            asset_root, registry = make_asset_root(root)

            with self.assertRaises(GuitarSpecialistError) as caught:
                resolve_specialist("", asset_root, registry=registry)

        self.assertEqual("specialist.invalid_id", caught.exception.code)

    def test_path_escaping_registration_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            outside = Path(root) / "outside.pt"
            outside.write_bytes(b"weights")
            asset_root, registry = make_asset_root(root, model_file="../outside.pt", create=False)

            with self.assertRaises(GuitarSpecialistError) as caught:
                resolve_specialist(SPECIALIST_ID, asset_root, registry=registry)

        self.assertEqual("specialist.path_escape", caught.exception.code)

    def test_symlinked_registration_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            asset_root, registry = make_asset_root(root, create=False)
            outside = Path(root) / "outside.pt"
            outside.write_bytes(b"weights")
            link = asset_root / "metal" / "lead_rhythm.pt"
            link.parent.mkdir(parents=True, exist_ok=True)
            try:
                link.symlink_to(outside)
            except (OSError, NotImplementedError):
                self.skipTest("This environment cannot create symbolic links.")

            with self.assertRaises(GuitarSpecialistError) as caught:
                resolve_specialist(SPECIALIST_ID, asset_root, registry=registry)

        self.assertEqual("specialist.path_escape", caught.exception.code)

    def test_missing_bytes_are_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            asset_root, registry = make_asset_root(root, create=False)

            with self.assertRaises(GuitarSpecialistError) as caught:
                resolve_specialist(SPECIALIST_ID, asset_root, registry=registry)

        self.assertEqual("specialist.asset_missing", caught.exception.code)

    def test_altered_bytes_are_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            asset_root, registry = make_asset_root(root, model_bytes=b"tampered", registered_bytes=b"weights")

            with self.assertRaises(GuitarSpecialistError) as caught:
                resolve_specialist(SPECIALIST_ID, asset_root, registry=registry)

        self.assertEqual("specialist.hash_mismatch", caught.exception.code)

    def test_network_requiring_specialist_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            asset_root, registry = make_asset_root(root, network_required=True)

            with self.assertRaises(GuitarSpecialistError) as caught:
                resolve_specialist(SPECIALIST_ID, asset_root, registry=registry)

        self.assertEqual("specialist.network_required", caught.exception.code)


class SeparateGuitarRolesTests(unittest.TestCase):
    def make_job(self, root, *, name="guitar.wav"):
        guitar = Path(root) / name
        guitar.write_bytes(b"guitar")
        return guitar, Path(root) / "cache" / "roles"

    def separate(self, guitar, output, asset_root, registry, runner, **overrides):
        return separate_guitar_roles(
            guitar,
            output,
            specialist_id=SPECIALIST_ID,
            asset_root=asset_root,
            runner=runner,
            cuda_probe=overrides.pop("cuda_probe", lambda: False),
            registry=registry,
            **overrides,
        )

    def test_unregistered_specialist_fails_before_reading_audio_or_creating_output(self):
        with tempfile.TemporaryDirectory() as root:
            guitar, output = self.make_job(root)
            runner = FakeSpecialist()

            with self.assertRaises(GuitarSpecialistError) as caught:
                self.separate(guitar, output, Path(root) / "assets", {}, runner)

            self.assertEqual("specialist.unregistered", caught.exception.code)
            self.assertEqual([], runner.commands)
            self.assertFalse(output.parent.exists())

    def test_admitted_specialist_publishes_exactly_the_two_role_stems(self):
        with tempfile.TemporaryDirectory() as root:
            guitar, output = self.make_job(root)
            asset_root, registry = make_asset_root(root)
            runner = FakeSpecialist()

            result = self.separate(guitar, output, asset_root, registry, runner)

            self.assertEqual(output.resolve(), result)
            self.assertEqual(set(ROLE_STEM_NAMES), {path.name for path in result.iterdir()})
            self.assertEqual(b"cpu:" + LEAD_STEM.encode(), (result / LEAD_STEM).read_bytes())
            self.assertEqual([], list(output.parent.glob(".roles.staging-*")))

    def test_command_is_argv_only_and_keeps_spaced_paths_in_one_element(self):
        with tempfile.TemporaryDirectory() as root:
            spaced = Path(root) / "my songs"
            spaced.mkdir()
            guitar = spaced / "isolated guitar.wav"
            guitar.write_bytes(b"guitar")
            output = spaced / "role cache"
            asset_root, registry = make_asset_root(root)
            runner = FakeSpecialist()

            self.separate(guitar, output, asset_root, registry, runner)

            command = runner.commands[0]
            self.assertEqual(sys.executable, command[0])
            self.assertEqual(["-m", ENTRYPOINT], command[1:3])
            self.assertEqual(str(guitar.resolve()), command[-1])
            self.assertNotIn("&", " ".join(command[:1]))
            self.assertEqual(1, sum(1 for part in command if part == str(guitar.resolve())))

    def test_cuda_is_selected_when_available(self):
        with tempfile.TemporaryDirectory() as root:
            guitar, output = self.make_job(root)
            asset_root, registry = make_asset_root(root)
            runner = FakeSpecialist()

            self.separate(guitar, output, asset_root, registry, runner, cuda_probe=lambda: True)

            command = runner.commands[0]
            self.assertEqual("cuda", command[command.index("--device") + 1])

    def test_incomplete_output_is_never_published(self):
        with tempfile.TemporaryDirectory() as root:
            guitar, output = self.make_job(root)
            asset_root, registry = make_asset_root(root)

            with self.assertRaises(GuitarSpecialistError) as caught:
                self.separate(guitar, output, asset_root, registry, FakeSpecialist(missing={RHYTHM_STEM}))

            self.assertEqual("specialist.output_incomplete", caught.exception.code)
            self.assertIn(RHYTHM_STEM, caught.exception.cause)
            self.assertFalse(output.exists())

    def test_nonzero_exit_reports_bounded_diagnostics_without_publication(self):
        with tempfile.TemporaryDirectory() as root:
            guitar, output = self.make_job(root)
            asset_root, registry = make_asset_root(root)
            noisy = "\n".join([f"discarded-{index}" for index in range(30)] + [f"detail-{index}" for index in range(6)])

            with self.assertRaises(GuitarSpecialistError) as caught:
                self.separate(guitar, output, asset_root, registry, FakeSpecialist(fail=True, output=noisy))

            cause = caught.exception.cause
            self.assertEqual("specialist.inference_failed", caught.exception.code)
            self.assertIn("CPU exited with code 9", cause)
            self.assertIn("detail-5", cause)
            self.assertNotIn("discarded-29", cause)
            self.assertLess(len(cause), 1000)
            self.assertFalse(output.exists())

    def test_cancellation_before_launch_publishes_nothing(self):
        with tempfile.TemporaryDirectory() as root:
            guitar, output = self.make_job(root)
            asset_root, registry = make_asset_root(root)
            runner = FakeSpecialist()
            cancellation = CancellationToken()
            cancellation.cancel()

            with self.assertRaises(GuitarSpecialistError) as caught:
                self.separate(guitar, output, asset_root, registry, runner, cancellation=cancellation)

            self.assertEqual("specialist.cancelled", caught.exception.code)
            self.assertEqual([], runner.commands)
            self.assertFalse(output.exists())

    def test_missing_guitar_input_fails_without_running_the_specialist(self):
        with tempfile.TemporaryDirectory() as root:
            asset_root, registry = make_asset_root(root)
            runner = FakeSpecialist()
            output = Path(root) / "cache" / "roles"

            with self.assertRaises(GuitarSpecialistError) as caught:
                self.separate(Path(root) / "missing.wav", output, asset_root, registry, runner)

            self.assertEqual("input.not_file", caught.exception.code)
            self.assertEqual([], runner.commands)
            self.assertFalse(output.parent.exists())


if __name__ == "__main__":
    unittest.main()
