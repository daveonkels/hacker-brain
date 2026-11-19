# Bounded-memory incremental refresh

Production source is maintained in `~/Dev/hacker-brain`. Commit and push before running `scripts/deploy-orion.sh` from that checkout. Orion receives a checksum-verified archive of the published commit; `/opt/hacker-brain/DEPLOYED_REVISION` identifies it. This deployment preserves the existing Orion service, Vault relay, model, systemd limits, and refresh timer settings. It runs the offline tests on Orion before installing, captures the previous source and completed data generation, and restores source on startup failure. It does not use the historical Luna unit installer.

## History and indexes

A refresh preserves the original collection start and every collected source ID. `corpus.sqlite3` stores each complete source object and its sample/context membership. The first refresh imports the existing NDJSON exports one record at a time; subsequent generations copy the database and apply only fetched changes. Exports remain complete and are streamed in ID order, including a streamed gzip copy. Existing deletion markers keep the same behavior: upstream deletions update that record rather than inventing historical revisions.

The keyword index is copied from the previous generation. New or changed records and their affected descendants are updated, so changed titles, ancestors, reparenting, deletion markers, and parent context remain consistent. Unchanged records are not reparsed or reindexed. The semantic index copies existing vectors and compares only affected model inputs. Missing passages are queued on disk and embedded in batches of four. Existing vectors are reused, and model/fingerprint/integrity checks still gate publication. Exports, database copies, hashes and coverage scans still perform work proportional to the collection's disk size; they do not load the whole history into memory.

## Process isolation and publication

The HTTP process starts a separate Python worker for collection and indexing. It does not pass Vault credentials or proxy settings to the worker. On Linux the worker has a 768 MiB address-space limit; exceeding it fails the refresh and preserves the live generation. The worker writes status atomically, with a shared inherited flock preventing concurrent refreshes. It prepares and validates the indexes but cannot publish them through this entry point. The parent waits for active readers, atomically switches the generation pointer and invalidates the old semantic engine, then applies the existing two-generation retention policy. Original seed history and the shared ranking cache remain intact. A restart kills both parent and child through the existing systemd control group; unfinished work resumes on the next refresh.

Search keeps vector reads in small batches and only the top 500 candidate bodies in memory. A single per-engine score cache avoids retaining old engines after publication. The score map still scales with the number of indexed comments; exact semantic retrieval is not constant-time or constant-space at unlimited scale. None of these changes drops old history.

## Verification and rollback

Run `python3 -m unittest discover -q`. Coverage includes history, ancestor edits/deletions, clean-rebuild equivalence, failed/resumed embedding, bounded source reads, a real child process, concurrent readers, and single-job locking.

For a live verification, request a refresh and measure both the parent and worker RSS, cgroup anonymous/file memory and pressure, HTTP status and keyword-search latency throughout the run. Confirm completion, unchanged historical IDs and window start, valid SQLite indexes, and a healthy external freshness monitor. Measure the first streaming import and a subsequent incremental run separately.

Rollback snapshots are under `/var/backups/hacker-brain/incremental-<commit>`. Stop the refresh timer and app, restore the `source` directory, and restart. Old code can read newly generated NDJSON and indexes; to restore the precise prior data generation, use `data-reference.json`, the independently saved `current.json`, and the hardlinked `generation` snapshot. Preserve the root ranking cache unless specifically recovering it. Restart the timer afterward. Do not delete runtime data during a code rollback.
