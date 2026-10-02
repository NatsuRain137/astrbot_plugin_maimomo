"""打包 AstrBot 插件为可安装 zip，并用 AstrBot 自身的逻辑验证压缩包合法性。"""

import hashlib
import os
import shutil
import sys
import zipfile
from pathlib import Path

# 本脚本放在插件目录的 tools/ 下（不参与打包）
PLUGIN_DIR = Path(__file__).resolve().parent.parent
WORKSPACE = PLUGIN_DIR.parent
DIST = WORKSPACE / "dist"
PLUGIN_NAME = PLUGIN_DIR.name

ASTRBOT_APP = Path(r"C:\Users\dell\AppData\Local\AstrBot\backend\app")
sys.path.insert(0, str(ASTRBOT_APP))

TMP_ROOT = WORKSPACE / ".pack_tmp_root"
(TMP_ROOT / "data").mkdir(parents=True, exist_ok=True)
os.environ["ASTRBOT_ROOT"] = str(TMP_ROOT)

import yaml  # noqa: E402
from astrbot.core.star.updator import PluginUpdator  # noqa: E402

VERSION = yaml.safe_load(
    (PLUGIN_DIR / "metadata.yaml").read_text(encoding="utf-8")
)["version"]

EXCLUDE_DIRS = {
    "__pycache__", ".git", ".astrbot_test_root", ".astrbot_pipe_root",
    ".astrbot_diag_root", ".venv", "venv", "tools", "dist",
}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".zip", ".tmp"}
EXCLUDE_NAMES = {".DS_Store"}


def collect_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        if path.suffix in EXCLUDE_SUFFIXES or path.name in EXCLUDE_NAMES:
            continue
        files.append(rel)
    return files


def build_zip(zip_path: Path, prefix: str) -> None:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for rel in collect_files(PLUGIN_DIR):
            entry = f"{prefix}{rel.as_posix()}" if prefix else rel.as_posix()
            zf.write(PLUGIN_DIR / rel, entry)


def simulate_install(zip_path: Path, label: str, sandbox: Path) -> None:
    """按 AstrBot 的安装流程解压，验证文件落在 target_dir 根目录（不嵌套）。"""
    target = sandbox / f"installed-{label}"
    target.mkdir(parents=True, exist_ok=True)
    PluginUpdator().unzip_file(str(zip_path), str(target))

    listing = sorted(p.name for p in target.iterdir())
    assert (target / "metadata.yaml").is_file(), f"{label}: 缺少 metadata.yaml"
    assert (target / "main.py").is_file(), f"{label}: 缺少 main.py"
    for required in ("_conf_schema.json", "requirements.txt", "service.py"):
        assert (target / required).is_file(), f"{label}: 缺少 {required}"
    nested = [
        p for p in target.iterdir()
        if p.is_dir() and (p / "metadata.yaml").is_file()
    ]
    assert not nested, f"{label}: 出现多余嵌套目录 {nested}"
    print(f"[OK] {label} 模拟安装成功，落地 {len(listing)} 项")
    assert not zip_path.exists(), f"{label}: 安装流程应已消费 zip"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    print(f"[..] 插件版本 {VERSION}，待打包 {len(collect_files(PLUGIN_DIR))} 个文件")
    outputs = [
        (DIST / f"{PLUGIN_NAME}-v{VERSION}.zip", "",
         "包根直接放 metadata.yaml（推荐）"),
        (DIST / f"{PLUGIN_NAME}-folder-v{VERSION}.zip", f"{PLUGIN_NAME}/",
         "外层套同名文件夹"),
    ]

    for zip_path, prefix, label in outputs:
        build_zip(zip_path, prefix)
        entry = PluginUpdator.validate_plugin_archive(str(zip_path))
        meta = PluginUpdator.inspect_plugin_archive(str(zip_path))["metadata"]
        assert meta["name"] == PLUGIN_NAME, meta["name"]
        assert meta["version"] == VERSION, meta["version"]
        size = zip_path.stat().st_size
        assert size < 16 * 1024 * 1024, "超过插件市场 16MB 限制"
        print(f"[OK] {zip_path.name}  {size:,} bytes  metadata={entry}  ({label})")

    # 校验和要在模拟安装之前算（安装流程会删掉传入的 zip）
    sums = DIST / "SHA256SUMS.txt"
    sums.write_text(
        "\n".join(f"{sha256(p)}  {p.name}" for p, _, _ in outputs) + "\n",
        encoding="utf-8",
    )
    print(f"[OK] 校验和已写入 {sums.name}")

    sandbox = WORKSPACE / ".pack_sim"
    shutil.rmtree(sandbox, ignore_errors=True)
    sandbox.mkdir(parents=True, exist_ok=True)
    try:
        for index, (zip_path, _, _) in enumerate(outputs):
            copy = sandbox / f"copy{index}.zip"
            shutil.copy2(zip_path, copy)
            simulate_install(copy, zip_path.stem, sandbox)
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    for zip_path, _, _ in outputs:
        assert zip_path.is_file(), f"产物丢失：{zip_path}"
    print("\n[OK] 打包完成，产物在 dist/")


if __name__ == "__main__":
    main()
