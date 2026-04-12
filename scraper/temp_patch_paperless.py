from pathlib import Path
path = Path('paperless_uploader.py')
text = path.read_text(encoding='utf-8')
old = '''        checksum = self.calculate_checksum(filepath)
        if not checksum:
            return

        old_checksum = self.checksums.get(doc_id)
        if old_checksum != checksum:
            log.info("Document %s has changed (old: %s, new: %s)", doc_id, old_checksum, checksum)
            self.upload_to_paperless(filepath, metadata)
            self.checksums[doc_id] = checksum
            self.save_checksums()
        else:
            log.debug("Document %s unchanged", doc_id)
'''
new = '''        checksum = self.calculate_checksum(filepath)
        if not checksum:
            return

        if self._upload_needed(doc_id, checksum):
            entry = self.checksums.get(doc_id)
            old_checksum = entry.get("checksum") if isinstance(entry, dict) else entry
            log.info("Document %s upload needed (old checksum=%s, new checksum=%s)", doc_id, old_checksum, checksum)
            success = self.upload_to_paperless(filepath, metadata)
            if success:
                self._mark_uploaded(doc_id, checksum)
            else:
                self._mark_failed(doc_id, checksum, "upload failed")
        else:
            log.debug("Document %s unchanged and already uploaded", doc_id)
'''
if old not in text:
    raise SystemExit('old block not found')
path.write_text(text.replace(old, new), encoding='utf-8')
