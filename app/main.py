"""최초 설정을 확인한 뒤 E-Class Quest Textual TUI를 실행하는 진입점."""

from __future__ import annotations

import argparse

from app.config import get_settings
from app.setup_store import LocalSetupStore, SetupStoreError
from app.setup_wizard import SetupCancelledError, run_setup_wizard
from app.tui.app import EclassQuestApp


def main(argv: list[str] | None = None) -> int:
    """필요하면 한 번만 설정을 입력받고 Textual TUI를 실행한다."""

    parser = argparse.ArgumentParser(prog="eclass-quest")
    parser.add_argument("--setup", action="store_true", help="저장된 사용자 설정을 다시 입력합니다.")
    args = parser.parse_args(argv)
    store = LocalSetupStore()
    try:
        if args.setup or not store.is_complete():
            run_setup_wizard(store, force=args.setup)
        settings = get_settings(store)
    except (SetupStoreError, SetupCancelledError) as exc:
        print(f"설정을 완료하지 못했습니다: {exc}")
        print("다시 설정하려면 사용하는 OS의 실행 명령에 --setup 옵션을 붙이세요.")
        return 2

    EclassQuestApp(settings).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
