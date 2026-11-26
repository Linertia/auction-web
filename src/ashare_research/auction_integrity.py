"""Shared identity and commit contract for live V2 finals."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

GATES = frozenset({'0915', '0920', '0924'})
FINAL_FILES = (
    'fixed-0925-v2.json', 'fixed-0925-v2-all.csv',
    'fixed-0925-v2-top30.csv', 'fixed-0925-v2-qualified.csv',
    'fixed-0925-v2-holding-observations.csv',
)


def select_snapshot_path(root: Path, day: str, stem: str) -> Path | None:
    """All readers share run identity; legacy days use the same deterministic rule."""
    folder = Path(root) / day
    pointer = folder / 'authoritative-run.json'
    if pointer.exists():
        identity = json.loads(pointer.read_text(encoding='utf-8-sig'))
        run_id = identity.get('run_id', '')
        if (identity.get('trade_date') != day
                or not re.fullmatch(r'run-v2-[A-Za-z0-9-]+', run_id)):
            raise ValueError('INVALID_RUN_IDENTITY')
        run = folder / run_id
        if not run.resolve().is_relative_to(folder.resolve()):
            raise ValueError('RUN_OUTSIDE_DATE_DIRECTORY')
        path = run / (stem + '.json')
        return path if path.is_file() else None
    paths = sorted(folder.glob('run-v2-*/' + stem + '.json'))
    return paths[-1] if paths else None


def validate_final(path: Path, *, day: str, prior: str = '') -> dict[str, Any]:
    """Fail closed. A readable JSON alone is never a committed realtime final."""
    errors: list[str] = []
    missing = [name for name in FINAL_FILES if not (path.parent / name).is_file()]
    payload: dict[str, Any] = {}
    try:
        payload = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as exc:
        errors.append(f'FINAL_JSON_INVALID:{type(exc).__name__}')
    if missing:
        errors.append('FINAL_FILES_MISSING')
    if payload.get('data_healthy') is not True:
        errors.append('DATA_UNHEALTHY')
    if set(payload.get('completed_gates', [])) != GATES or payload.get('gates_complete') is not True:
        errors.append('GATES_INCOMPLETE')
    if payload.get('final_acceptable') is not True or payload.get('realtime_final') is not True:
        errors.append('NOT_ACCEPTABLE_REALTIME_FINAL')
    readiness = payload.get('history_readiness') or {}
    if (readiness.get('ready') is not True or readiness.get('coverage', 0) < .98
            or readiness.get('source') != 'explicit_trade_calendar'
            or not re.fullmatch(r'\d{8}', str(readiness.get('trade_date', '')))
            or readiness.get('trade_date', day) >= day
            or (prior and readiness.get('trade_date') != prior)):
        errors.append('D1_NOT_READY')
    try:
        observed = datetime.fromisoformat(payload.get('observed_at', ''))
        if observed.tzinfo is None or observed.astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d') != day:
            errors.append('FINAL_DATE_MISMATCH')
    except (TypeError, ValueError):
        errors.append('FINAL_TIME_INVALID')
    if payload.get('run_id') != path.parent.name or payload.get('snapshot_kind') != 'fixed_0925_v2':
        errors.append('FINAL_IDENTITY_MISMATCH')
    hashes = payload.get('artifact_sha256') or {}
    for name in FINAL_FILES[1:]:
        item = path.parent / name
        if not item.is_file():
            continue
        if hashes.get(name) != hashlib.sha256(item.read_bytes()).hexdigest():
            errors.append('ARTIFACT_HASH_MISMATCH:' + name)
    return {'success': not errors, 'data_healthy': payload.get('data_healthy') is True,
            'gates_complete': payload.get('gates_complete') is True,
            'final_acceptable': payload.get('final_acceptable') is True,
            'run_directory': str(path.parent.resolve()), 'missing_files': missing,
            'errors': errors,
            'latest_final_attempt_path': str(path.with_name('latest-final-attempt-v2.json').resolve())}


def validate_public_final(proof: Any, day: str) -> dict[str, Any]:
    allowed = {'schema_version', 'gates_complete', 'history_ready', 'realtime_final',
               'prior_trade_date', 'history_coverage', 'source_sha256'}
    if not isinstance(proof, dict) or set(proof) != allowed:
        raise ValueError('FINAL_VALIDATION_MISSING_OR_INVALID')
    if (proof['schema_version'] != 1 or any(proof[key] is not True for key in
            ('gates_complete', 'history_ready', 'realtime_final'))
            or not isinstance(proof['history_coverage'], (int, float))
            or not .98 <= proof['history_coverage'] <= 1
            or not re.fullmatch(r'\d{8}', str(proof['prior_trade_date']))
            or proof['prior_trade_date'] >= day
            or not re.fullmatch(r'[0-9a-f]{64}', str(proof['source_sha256']))):
        raise ValueError('FINAL_VALIDATION_REJECTED')
    return proof
