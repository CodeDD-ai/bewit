from archrev.globmatch import matches, matches_any, normalize


def test_globstar_spans_directories():
    assert matches("django/**/migrations/**", "django/app/migrations/0001.py")
    assert matches("type-db/**/*.tql", "type-db/schema/define.tql")
    assert matches("type-db/**/*.tql", "type-db/define.tql")  # zero segments
    assert not matches("type-db/**/*.tql", "django/define.tql")


def test_single_star_stays_in_segment():
    assert matches("app/*.py", "app/main.py")
    assert not matches("app/*.py", "app/sub/main.py")


def test_basename_patterns_match_any_depth():
    assert matches(".env*", ".env")
    assert matches(".env*", "django/.env.local")
    assert matches("Dockerfile*", "services/api/Dockerfile.prod")


def test_case_insensitive_and_separator_agnostic():
    assert matches("K8S/**", "k8s/base/deploy.yaml")
    assert matches("db/migrations/**", "db\\migrations\\0001.sql")


def test_trailing_globstar_and_exact():
    assert matches("docs/**", "docs/a/b/c.md")
    assert matches("VERSION", "VERSION")
    assert not matches("VERSION", "sub/VERSION.bak")


def test_empty_pattern_matches_nothing():
    assert not matches("", "anything")


def test_matches_any():
    assert matches_any(["a/**", "b/**"], "b/x.txt")
    assert not matches_any(["a/**", "b/**"], "c/x.txt")


def test_normalize():
    assert normalize("./a\\b/c") == "a/b/c"
    assert normalize("/a/b/") == "a/b"
