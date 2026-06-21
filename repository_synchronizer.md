## Synchronize GitHub and Gitea

`repository_synchronizer.py` reads the JSON comparison report, checks the current remote branches again, and creates a safe bidirectional synchronization plan. It copies missing branches and fast-forwards whichever server is behind. Diverged and unrelated branches are reported but never force-pushed.

The synchronizer requires Git LFS in addition to the comparison-tool requirements. Confirm it is installed with:

```bash
git lfs version
```

Generate a report, then run the synchronizer without `--apply` to inspect the plan:

```bash
python3 repository_comparator.py
python3 repository_synchronizer.py
```

The dry-run writes `reports/repository-sync-plan.md` and `reports/repository-sync-plan.json`. After reviewing them, apply the safe actions explicitly:

```bash
python3 repository_synchronizer.py --apply
```

Apply mode writes `reports/repository-sync-result.md` and `reports/repository-sync-result.json`. Repositories missing from one server are created with the source visibility, description, default branch, and archived state. Git LFS objects are uploaded before their branch is pushed.

Useful options:

```text
--report PATH           Comparison JSON (default: reports/repository-comparison.json)
--output-dir PATH       Audit report directory (default: reports)
--apply                 Perform the planned remote changes
--allow-incomplete      Process valid entries from an incomplete comparison report
--no-progress           Disable progress output
```

The tool never deletes branches, force-pushes, or automatically resolves diverged histories. Tokens used for synchronization need repository creation and write permissions on both servers.
