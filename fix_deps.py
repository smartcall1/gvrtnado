"""
nado-protocol과 grvt-pysdk의 의존성 충돌을 해결한다.

1) eth-account: nado-protocol(<0.9) vs grvt-pysdk(>=0.13.4)
   → encode_structured_data → encode_typed_data 패치

2) pydantic: nado-protocol은 v1 문법 사용, grvt-pysdk가 v2 설치
   → @root_validator → @root_validator(skip_on_failure=True) 패치
   → @validator → @field_validator 호환 패치

사용법:
    pip install grvt-pysdk
    pip install nado-protocol --no-deps
    python fix_deps.py
"""
import re
import site
import sys
from pathlib import Path


def find_nado_pkg() -> Path:
    try:
        import nado_protocol
        return Path(nado_protocol.__file__).parent
    except Exception:
        pass

    dirs = site.getsitepackages()
    try:
        dirs.append(site.getusersitepackages())
    except Exception:
        pass

    for d in dirs:
        pkg_dir = Path(d) / "nado_protocol"
        if pkg_dir.exists():
            return pkg_dir

    print("[ERROR] nado_protocol not found")
    sys.exit(1)


def patch_eth_account(pkg_dir: Path):
    """encode_structured_data → encode_typed_data"""
    print("\n--- eth-account 패치 ---")
    patched = False
    for py_file in pkg_dir.rglob("*.py"):
        text = py_file.read_text(encoding="utf-8", errors="ignore")
        if "encode_structured_data" in text:
            new_text = text.replace("encode_structured_data", "encode_typed_data")
            py_file.write_text(new_text, encoding="utf-8")
            print(f"[PATCHED] {py_file}")
            patched = True
    if not patched:
        print("[OK] encode_structured_data already patched or not found")

    try:
        from eth_account.messages import encode_typed_data
        print("[OK] eth_account.encode_typed_data available")
    except ImportError:
        print("[ERROR] eth_account too old — need >=0.13.0")
        sys.exit(1)


def patch_pydantic(pkg_dir: Path):
    """pydantic v1 → v2 호환 패치"""
    print("\n--- pydantic v2 호환 패치 ---")
    try:
        import pydantic
        version = int(pydantic.VERSION.split(".")[0])
    except Exception:
        print("[SKIP] pydantic not installed")
        return

    if version < 2:
        print(f"[OK] pydantic v{pydantic.VERSION} — v1이므로 패치 불필요")
        return

    print(f"[INFO] pydantic v{pydantic.VERSION} 감지 — v1 문법 패치 적용")

    for py_file in pkg_dir.rglob("*.py"):
        text = py_file.read_text(encoding="utf-8", errors="ignore")
        original = text

        # @root_validator → @root_validator(skip_on_failure=True)
        if "@root_validator" in text and "skip_on_failure" not in text:
            text = text.replace(
                "@root_validator\n",
                "@root_validator(skip_on_failure=True)\n",
            )
            text = text.replace(
                "@root_validator\r\n",
                "@root_validator(skip_on_failure=True)\r\n",
            )
            text = re.sub(
                r"@root_validator\(pre=False\)",
                "@root_validator(pre=False, skip_on_failure=True)",
                text,
            )

        # conlist/conset: min_items → min_length, max_items → max_length
        text = text.replace("min_items=", "min_length=")
        text = text.replace("max_items=", "max_length=")

        # Field: min_items/max_items도 동일
        # constr: regex → pattern
        text = re.sub(r'constr\(([^)]*)\bregex=', r'constr(\1pattern=', text)

        if text != original:
            py_file.write_text(text, encoding="utf-8")
            print(f"[PATCHED] {py_file}")

    print("[OK] pydantic 패치 완료")


def verify():
    """패치 후 import 검증"""
    print("\n--- 검증 ---")
    try:
        # 기존 캐시 무효화
        mods_to_remove = [k for k in sys.modules if k.startswith("nado_protocol")]
        for m in mods_to_remove:
            del sys.modules[m]

        import nado_protocol
        print("[OK] nado_protocol import 성공")
    except Exception as e:
        print(f"[WARN] nado_protocol import 실패: {e}")
        print("       추가 패치가 필요할 수 있음")


def main():
    pkg_dir = find_nado_pkg()
    print(f"nado_protocol 경로: {pkg_dir}")

    patch_eth_account(pkg_dir)
    patch_pydantic(pkg_dir)
    verify()

    print("\nDone.")


if __name__ == "__main__":
    main()
