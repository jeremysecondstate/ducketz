"""Offline exact-file sharing checks. No application, network or datastore use."""
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools import atlas_datastore_manager as manager


class DatastoreManagerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def objects(self, raw=b'{"observations":[1,2,3]}'):
        name = hashlib.sha256(raw).hexdigest() + ".json"
        paths = [self.root / "intraday/source-objects" / name,
                 self.root / "intraday/market-inputs/raw/2026/10/29" / name]
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        return paths

    def proposal(self):
        return manager.plan(self.root, manager.inventory(self.root))

    def evidence(self, proposal):
        paths = sorted({item[key] for item in proposal['pairs'] for key in ('canonical', 'duplicate')})
        path = self.root / 'reviewed-quiescence.json'
        path.write_text(json.dumps({'fixture': 'inactive paths reviewed offline', 'paths': paths}))
        return {'paths': paths, 'evidence': str(path),
                'evidence_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}

    def apply(self, proposal):
        return manager.apply(self.root, proposal, quiescent=True,
                             quiescence_evidence=self.evidence(proposal))

    def test_shares_exact_objects_preserving_paths_and_unique_state(self):
        first, second = self.objects()
        database = self.root / "intraday/shadow.sqlite3"
        database.write_bytes(b'not a real database; must remain untouched')
        before = database.read_bytes()
        proposal = self.proposal()
        self.assertEqual(len(proposal['pairs']), 1)
        result = self.apply(proposal)
        self.assertEqual(result['results'][0]['status'], 'shared')
        self.assertTrue(os.path.samefile(first, second))
        self.assertEqual(first.read_bytes(), b'{"observations":[1,2,3]}')
        self.assertEqual(database.read_bytes(), before)
        self.assertEqual(len(self.proposal()['pairs']), 0)
        self.assertEqual(self.apply(proposal)['results'][0]['status'], 'already_shared')

    def test_changed_content_is_not_replaced(self):
        first, second = self.objects()
        proposal = self.proposal()
        second.write_bytes(b'new independent observation')
        result = self.apply(proposal)
        self.assertEqual(result['results'][0]['status'], 'pending')
        self.assertEqual(second.read_bytes(), b'new independent observation')
        self.assertFalse(os.path.samefile(first, second))

    def test_hash_named_corrupt_object_is_rejected(self):
        _, second = self.objects()
        second.write_bytes(b'{"observations":[3,2,1]}')
        proposal = self.proposal()
        self.assertEqual(proposal['pairs'], [])
        self.assertEqual(len(proposal['rejected']), 1)

    def test_non_hash_files_and_manifests_are_not_deduplicated(self):
        raw = b'{}'
        name = hashlib.sha256(raw).hexdigest() + '.json'
        for relative in ['intraday/system-1a/ready/snapshots/manifests/' + name,
                         'intraday/system-1b/models/model.pkl',
                         'intraday/execution/receipt.json']:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        self.assertEqual(manager.inventory(self.root)['candidate_groups'], [])

    def test_bounded_inventory_and_plan_do_not_claim_complete_coverage(self):
        self.objects()
        self.assertFalse(manager.inventory(self.root, max_files=1)['coverage_complete'])
        proposal = manager.plan(self.root, manager.inventory(self.root), max_hash_bytes=1)
        self.assertFalse(proposal['coverage_complete'])
        self.assertEqual(proposal['pairs'], [])

    def test_path_traversal_plan_is_rejected(self):
        self.objects()
        proposal = self.proposal()
        proposal['pairs'][0]['duplicate'] = '../outside.json'
        result = self.apply(proposal)
        self.assertEqual(result['results'][0]['status'], 'pending')

    def test_symlink_paths_are_not_followed(self):
        first, second = self.objects()
        second.unlink()
        try:
            second.symlink_to(first)
        except (OSError, NotImplementedError):
            self.skipTest('OS did not grant symlink creation')
        with self.assertRaises(ValueError):
            manager.safe_file(self.root, second.relative_to(self.root))
        self.assertFalse(manager.inventory(self.root)['coverage_complete'])

    def test_change_during_link_preparation_preserves_new_content(self):
        first, second = self.objects()
        proposal = self.proposal()
        original_link = os.link

        def race(source, target):
            original_link(source, target)
            second.write_bytes(b'revised before replacement')

        with patch.object(manager.os, 'link', side_effect=race):
            result = self.apply(proposal)
        self.assertEqual(result['results'][0]['status'], 'pending')
        self.assertEqual(second.read_bytes(), b'revised before replacement')
        self.assertFalse(os.path.samefile(first, second))
        self.assertEqual(list(second.parent.glob('*.link')), [])

    def test_maintenance_is_exclusive(self):
        with manager.maintenance_lock(self.root):
            with self.assertRaises(OSError):
                with manager.maintenance_lock(self.root):
                    self.fail('competing cleanup entered')

    def test_apply_requires_selected_path_quiescence(self):
        self.objects()
        with self.assertRaisesRegex(ValueError, 'quiescence'):
            manager.apply(self.root, self.proposal())

    def test_canonical_change_during_link_preserves_valid_duplicate(self):
        first, second = self.objects()
        original = second.read_bytes()
        proposal, link = self.proposal(), os.link

        def race(source, target):
            link(source, target)
            first.write_bytes(b'changed canonical must not replace valid duplicate')

        with patch.object(manager.os, 'link', side_effect=race):
            result = self.apply(proposal)
        self.assertEqual(result['results'][0]['status'], 'pending')
        self.assertEqual(second.read_bytes(), original)

    def test_duplicate_change_at_staging_is_preserved(self):
        first, second = self.objects()
        proposal, rename = self.proposal(), os.rename
        changed = b'unique replacement immediately before staging'

        def race(source, target):
            Path(source).write_bytes(changed)
            rename(source, target)

        with patch.object(manager.os, 'rename', side_effect=race):
            result = self.apply(proposal)
        self.assertEqual(result['results'][0]['status'], 'pending')
        self.assertEqual(second.read_bytes(), changed)
        self.assertTrue(Path(result['results'][0]['recovery_receipt']).is_file())
        self.assertFalse(os.path.samefile(first, second))

    def test_new_writer_after_staging_is_preserved_with_original_receipt(self):
        first, second = self.objects()
        original = second.read_bytes()
        proposal, rename = self.proposal(), os.rename

        def race(source, target):
            rename(source, target)
            Path(source).write_bytes(b'new concurrent writer')

        with patch.object(manager.os, 'rename', side_effect=race):
            result = self.apply(proposal)
        item = result['results'][0]
        self.assertEqual(item['status'], 'pending')
        self.assertEqual(second.read_bytes(), b'new concurrent writer')
        self.assertEqual(Path(item['retained_original']).read_bytes(), original)
        self.assertTrue(Path(item['recovery_receipt']).is_file())

    def test_crash_after_staging_is_recoverable_without_overwrite(self):
        _, second = self.objects()
        original = second.read_bytes()
        proposal, rename = self.proposal(), os.rename

        def crash(source, target):
            rename(source, target)
            raise SystemExit('simulated process death')

        with patch.object(manager.os, 'rename', side_effect=crash):
            with self.assertRaises(SystemExit):
                self.apply(proposal)
        self.assertFalse(second.exists())
        result = self.apply(proposal)
        self.assertEqual(result['results'][0]['status'], 'pending')
        self.assertEqual(second.read_bytes(), original)
        result = self.apply(self.proposal())
        self.assertEqual(result['results'][0]['status'], 'shared')

    def test_quiescence_evidence_must_match_exact_paths_and_hash(self):
        self.objects()
        proposal = self.proposal()
        proof = self.evidence(proposal)
        proof['paths'] = []
        with self.assertRaisesRegex(ValueError, 'scoped'):
            manager.apply(self.root, proposal, quiescent=True, quiescence_evidence=proof)
        proof = self.evidence(proposal)
        Path(proof['evidence']).write_text('changed evidence')
        with self.assertRaisesRegex(ValueError, 'evidence changed'):
            manager.apply(self.root, proposal, quiescent=True, quiescence_evidence=proof)

    def test_crash_after_link_publication_resumes_retained_original_cleanup(self):
        first, second = self.objects()
        proposal, link = self.proposal(), os.link

        def crash(source, target):
            link(source, target)
            if Path(target) == second:
                raise SystemExit('simulated death after new reader path published')

        with patch.object(manager.os, 'link', side_effect=crash):
            with self.assertRaises(SystemExit):
                self.apply(proposal)
        self.assertTrue(os.path.samefile(first, second))
        result = self.apply(proposal)
        self.assertEqual(result['results'][0]['status'], 'shared_recovered')
        self.assertEqual(list((self.root / manager.RECOVERY_ROOT / 'staging').glob('*.raw')), [])

    def test_dangling_reparse_recovery_or_lock_path_is_rejected(self):
        original = Path.lstat
        for relative in ('locks', manager.RECOVERY_ROOT + '/staging'):
            target = self.root / relative
            def reparse(path, *args, **kwargs):
                if path == target:
                    return SimpleNamespace(st_mode=stat.S_IFLNK, st_file_attributes=1024)
                return original(path, *args, **kwargs)
            with self.subTest(relative=relative), patch.object(Path, 'lstat', reparse):
                with self.assertRaisesRegex(ValueError, 'redirected'):
                    manager.recovery_path(self.root, relative + '/file')
                if relative == 'locks':
                    with self.assertRaisesRegex(ValueError, 'redirected'):
                        with manager.maintenance_lock(self.root):
                            self.fail('entered redirected maintenance path')


if __name__ == '__main__':
    unittest.main()
