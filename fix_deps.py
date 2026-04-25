"""
nado-protocol과 grvt-pysdk의 eth-account 버전 충돌을 해결한다.

nado-protocol: eth-account <0.9.0 요구
grvt-pysdk:    eth-account >=0.13.4 요구

실제 비호환은 encode_structured_data → encode_typed_data 함수명 변경뿐.
이 스크립트가 설치된 nado_protocol 소스를 자동 패치한다.

사용법:
    pip install grvt-pysdk
    pip install nado-protocol --no-deps
    python fix_deps.py
"""
import importlib
import site
import sys
from pathlib import Path


def find_nado_sign():
    try:
        import nado_protocol
        pkg_dir = Path(nado_protocol.__file__).parent
    except ImportError:
        for d in site.getsitepackages() + [site.getusersitepackages()]:
            pkg_dir = Path(d) / "nado_protocol"
            if pkg_dir.exists():
                break
        else:
            print("[ERROR] nado_protocol not found")
            sys.exit(1)

    sign_py = pkg_dir / "contracts" / "eip712" / "sign.py"
    if not sign_py.exists():
        for p in pkg_dir.rglob("sign.py"):
            if "eip712" in str(p):
                sign_py = p
                break

    return sign_py


def patch_file(path: Path):
    text = path.read_text(encoding="utf-8")

    if "encode_typed_data" in text:
        print(f"[OK] Already patched: {path}")
        return False

    if "encode_structured_data" not in text:
        print(f"[SKIP] No encode_structured_data found in: {path}")
        return False

    patched = text.replace("encode_structured_data", "encode_typed_data")
    path.write_text(patched, encoding="utf-8")
    print(f"[PATCHED] {path}")
    return True


def main():
    sign_py = find_nado_sign()
    if sign_py and sign_py.exists():
        patch_file(sign_py)
    else:
        print("[WARN] sign.py not found, searching all nado_protocol .py files...")
        try:
            import nado_protocol
            pkg_dir = Path(nado_protocol.__file__).parent
        except ImportError:
            print("[ERROR] nado_protocol not installed")
            sys.exit(1)

        found = False
        for py_file in pkg_dir.rglob("*.py"):
            text = py_file.read_text(encoding="utf-8", errors="ignore")
            if "encode_structured_data" in text:
                patch_file(py_file)
                found = True
        if not found:
            print("[OK] No files need patching")

    try:
        from eth_account.messages import encode_typed_data
        print("[OK] eth_account.encode_typed_data available")
    except ImportError:
        print("[ERROR] eth_account too old — need >=0.13.0")
        sys.exit(1)

    print("\nDone. Dependencies are now compatible.")


if __name__ == "__main__":
    main()
