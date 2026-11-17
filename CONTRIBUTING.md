# Contributing to SafePay

Thank you for contributing to SafePay. Please review the guidelines below to set up your environment, follow our development workflow, and submit changes.

---

## 1. Development Environment Setup

### Prerequisites
- Python 3.10+
- PostgreSQL (or local SQLite for lightweight testing)
- Redis (for Celery and caching)

### Steps
1. Clone the repository and navigate into the project root:
   ```bash
   git clone https://github.com/SimretAbebe/SafePay.git
   cd SafePay
   ```

2. Create and activate a virtual environment:
   ```bash
   python -m venv venv
   # On Windows (bash):
   source venv/Scripts/activate
   # On macOS/Linux:
   source venv/bin/activate
   ```

3. Install runtime and development dependencies:
   ```bash
   pip install -r requirements.txt
   pip install -r requirements-dev.txt
   ```

4. Configure your local environment:
   ```bash
   cp .env.example .env
   # Edit .env with your local settings if needed
   ```

5. Run database migrations:
   ```bash
   python manage.py migrate
   ```

---

## 2. Running Tests & Linters

Before submitting any code, ensure all system checks and tests pass cleanly:

- **Django system check:**
  ```bash
  python manage.py check
  ```

- **Run test suite:**
  ```bash
  python manage.py test payments
  ```

- **Run linter:**
  ```bash
  ruff check .
  ```

- **Validate OpenAPI schema:**
  ```bash
  python manage.py spectacular --validate
  ```

---

## 3. Commit Convention

We follow conventional commits consistent with the repository's commit history:
- `feat:` new user-facing or technical capability (e.g. `feat: add payment simulator`)
- `fix:` bug fixes (e.g. `fix: handle edge case in webhook signature verification`)
- `chore:` maintenance, dependencies, and configuration (e.g. `chore: update dependencies`)
- `docs:` documentation additions and edits (e.g. `docs: update code map`)
- `test:` test suite additions or improvements (e.g. `test: add provider routing unit tests`)

---

## 4. Opening a Pull Request

1. Create a feature branch from an up-to-date `main`:
   ```bash
   git checkout -b feat/your-feature-name
   ```
2. Make concise, purposeful commits following the commit convention.
3. Push your branch to GitHub:
   ```bash
   git push -u origin feat/your-feature-name
   ```
4. Open a pull request against `main` on GitHub detailing:
   - Summary of changes
   - Test cases and verification steps
   - Associated issue numbers (if any)
