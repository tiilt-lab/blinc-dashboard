"""Alembic chain contracts (infra audit H3 / M1, Phase 2 item 16).

The chain could not rebuild production: a duplicate create_table, a column
and indexes only ever added by hand, and a history nobody could replay on an
empty database. These read the migration sources (no Alembic or Flask under
CI's python-tests job) and pin the shape of the chain plus the guards the
reconciling migrations rely on. The DDL itself is exercised by the
`migrations` CI job against a real MySQL 8 (run_migrations.py roundtrip).
"""
import ast
import os
import re

ROOT = os.path.join(os.path.dirname(__file__), "..")
MIGRATIONS = os.path.join(ROOT, "src", "server", "migrations")
VERSIONS = os.path.join(MIGRATIONS, "versions")
TABLES = os.path.join(ROOT, "src", "server", "tables")

RECONCILE = "1f2e3d4c5b6a"
CASCADES = "2a3b4c5d6e7f"
LIVE_VIDEO = "3b4c5d6e7f80"  # session.live_video_analytics (record now, analyse later)
NEGOTIATION = "4c5d6e7f8091"  # session.negotiation_coding + negotiation_coding_run/negotiation_code


def _read(*parts):
    with open(os.path.join(ROOT, *parts)) as f:
        return f.read()


def _revisions():
    # {revision: (down_revision(s) as tuple, filename)} parsed from the module
    # constants, so the files never have to be imported.
    found = {}
    for fn in sorted(os.listdir(VERSIONS)):
        if not fn.endswith(".py"):
            continue
        with open(os.path.join(VERSIONS, fn)) as f:
            tree = ast.parse(f.read(), fn)
        consts = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                    and isinstance(node.targets[0], ast.Name):
                try:
                    consts[node.targets[0].id] = ast.literal_eval(node.value)
                except ValueError:
                    pass
        assert "revision" in consts, "%s: no revision constant" % fn
        assert "down_revision" in consts, "%s: no down_revision constant" % fn
        down = consts["down_revision"]
        if down is None:
            down = ()
        elif isinstance(down, str):
            down = (down,)
        assert consts["revision"] not in found, \
            "duplicate revision id %s (%s and %s)" % (consts["revision"], fn, found[consts["revision"]][1])
        found[consts["revision"]] = (tuple(down), fn)
    return found


def test_every_down_revision_resolves():
    revs = _revisions()
    for rev, (downs, fn) in revs.items():
        for d in downs:
            assert d in revs, "%s: down_revision %s does not exist" % (fn, d)


def test_single_root_and_single_head():
    revs = _revisions()
    roots = [r for r, (downs, _) in revs.items() if not downs]
    assert roots == ["d7cd8fa96963"], "expected one root, got %s" % roots
    referenced = {d for downs, _ in revs.values() for d in downs}
    heads = sorted(set(revs) - referenced)
    assert heads == [NEGOTIATION], "chain must have exactly one head: %s" % heads


def test_phase2_migrations_are_chained_after_phase1():
    revs = _revisions()
    assert revs[RECONCILE][0] == ("f8ae4e72c79c",)
    assert revs[CASCADES][0] == (RECONCILE,)
    assert revs[LIVE_VIDEO][0] == (CASCADES,)
    assert revs[NEGOTIATION][0] == (LIVE_VIDEO,)


def _version_source(rev):
    for fn in os.listdir(VERSIONS):
        if fn.startswith(rev + "_"):
            return _read("src", "server", "migrations", "versions", fn)
    raise AssertionError("no migration file for %s" % rev)


def test_duplicate_keyword_usage_creation_is_guarded():
    # d7cd8fa96963 (root) creates keyword_usage; 7da13891981c created it
    # again, which failed on every empty database.
    s = _version_source("7da13891981c")
    guard = s.index("has_table('keyword_usage')")
    create = s.index("op.create_table('keyword_usage'")
    assert guard < create, "keyword_usage create_table must be behind a has_table guard"
    assert "op.drop_table('keyword_usage')" not in s, \
        "7da13891981c must not drop keyword_usage; the root migration owns it"


def test_reconcile_migration_guards_every_step():
    s = _version_source(RECONCILE)
    assert "_has_column('session_device', 'posthoc_analyzed_date')" in s
    assert "has_table('user_system_interaction')" in s
    for name in ("idx_transcript_device_start", "idx_svm_device_time"):
        assert name in s, "reconcile migration must add %s" % name
    for chk in ("device_chk_1", "session_device_chk_3", "transcript_chk_1", "user_chk_2"):
        assert chk in s, "reconcile migration must add CHECK %s" % chk
    # Every creation is conditional on the inspector, so prod is a no-op.
    upgrade = s[s.index("def upgrade():"):s.index("def downgrade():")]
    for line in upgrade.splitlines():
        if "op.create_" in line or "ADD COLUMN" in line or "ADD CONSTRAINT" in line:
            assert re.match(r"\s{8,}", line), \
                "unguarded step in reconcile migration: %s" % line.strip()
    assert "unique_session_name" not in upgrade, \
        "unique_session_name never existed live; do not add it"


# (table, column, rule) the cascade migration must set and the model declare.
RULES = [
    ("session_device", "session_id", "CASCADE"),
    ("transcript", "session_device_id", "CASCADE"),
    ("keyword_usage", "transcript_id", "CASCADE"),
    ("keyword", "session_id", "CASCADE"),
    ("topic_model", "owner_id", "CASCADE"),
    ("keyword_list_item", "keyword_list_id", "CASCADE"),
    ("api_client", "user_id", "CASCADE"),
    ("llm_feedback_report", "session_id", "CASCADE"),
    ("llm_question_answer", "session_id", "CASCADE"),
    ("session_synthesized_report", "session_id", "CASCADE"),
    ("session", "topic_model_id", "SET NULL"),
    ("session", "folder", "SET NULL"),
    ("folder", "parent", "SET NULL"),
    ("speaker_transcript_metrics", "speaker_id", "SET NULL"),
]


def test_cascade_migration_sets_every_rule_idempotently():
    s = _version_source(CASCADES)
    for table, column, rule in RULES:
        assert re.search(r"\('%s', '[a-z0-9_]+', '%s', '[a-z_]+', '%s'" % (table, column, rule), s), \
            "cascade migration lacks %s.%s %s" % (table, column, rule)
    # A FK that already carries the rule is skipped (re-runnable on prod).
    assert "_current_rule(" in s and "if current == (rule or 'NO ACTION'):" in s
    # session.owner_id stays a plain FK on purpose.
    assert "('session', 'session_ibfk_1'" not in s


def test_models_declare_the_same_rules():
    for table, column, rule in RULES:
        src = _read("src", "server", "tables", table + ".py")
        pattern = r"%s\s*=\s*db\.Column\([^\n]*ForeignKey\('[a-z_]+\.id',\s*ondelete='%s'\)" % (column, rule)
        assert re.search(pattern, src), "%s.%s must declare ondelete='%s'" % (table, column, rule)


def test_models_declare_the_live_composite_indexes():
    assert "db.Index('idx_transcript_device_start', 'session_device_id', 'start_time')" in _read("src", "server", "tables", "transcript.py")
    assert "db.Index('idx_svm_device_time', 'session_device_id', 'time_stamp')" in _read("src", "server", "tables", "speaker_video_metrics.py")
    assert "unique_session_name" not in _read("src", "server", "tables", "session_device.py").split("__table_args__")[1]


def test_env_ignores_model_less_tables_without_flask():
    env = _read("src", "server", "migrations", "env.py")
    assert "def include_object(" in env
    assert env.count("include_object=include_object") == 2, "both offline and online configure must pass include_object"
    for t in ("seven_cs_analysis", "seven_cs_coded_segment", "concept_session", "concept_node",
              "concept_edge", "concept_cluster", "cluster_node_mapping", "llm_metrics",
              "user_system_interaction"):
        assert "'%s'" % t in env, "env.py MODEL_LESS_TABLES lacks %s" % t
    assert "except (ImportError, RuntimeError)" in env, "env.py must run without a Flask app"


def test_runner_does_not_import_the_app():
    s = _read("src", "server", "migrations", "run_migrations.py")
    assert not re.search(r"^\s*(from|import)\s+(app|flask|database|config)\b", s, re.M)
    assert "command.upgrade(cfg, 'head')" in s and "command.downgrade(cfg, 'base')" in s


def test_ci_runs_the_chain_on_mysql8():
    ci = _read(".github", "workflows", "ci.yml")
    assert "image: mysql:8" in ci
    assert "python src/server/migrations/run_migrations.py roundtrip" in ci
    assert "DC_DATABASE_URL:" in ci


def test_database_deletes_rely_on_cascades():
    s = _read("src", "server", "database.py")
    body = s[s.index("def delete_session("):s.index("def update_session(")]
    assert body.count("db.session.query(") == 1, "delete_session must be a single DELETE"
    assert "def _delete_folder_bulk" not in s


def test_token_retention_runs_daily():
    s = _read("src", "server", "scheduled_tasks.py")
    assert "delete_expired_account_tokens(TOKEN_RETENTION)" in s
    assert "TOKEN_RETENTION = timedelta(days=30)" in s
    assert "timedelta(days=1)" in s, "purge must be throttled to once a day"
    assert "_purge_account_tokens_daily()" in s[s.index("def check_transcripts"):s.index("def _purge_account_tokens_daily")]
    d = _read("src", "server", "database.py")
    assert "def delete_expired_account_tokens(max_age):" in d
    # only used/expired rows, never live tokens
    assert "AccountToken.used_at.isnot(None), AccountToken.expires_at < now" in d


def test_config_honours_database_name_override():
    assert "os.environ.get('DC_DATABASE_NAME')" in _read("src", "server", "config.py")
