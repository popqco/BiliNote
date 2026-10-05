"""打包回归测试：chromadb 动态加载子模块必须在静态导入图中可达。

背景（2026-10-05 AI 问答全灭复盘）：chromadb 1.x 把 API 实现与 telemetry
实现都写成配置字符串（chroma_api_impl="chromadb.api.rust.RustBindingsAPI"、
chroma_product_telemetry_impl="chromadb.telemetry.product.posthog.Posthog"），
运行时动态加载。PyInstaller 静态分析发现不了 → 打包后的 exe 里
`import chromadb` 成功，但第一次 PersistentClient 初始化就炸：
"No module named 'chromadb.telemetry.product.posthog'" /
"No module named 'chromadb.api.rust'"。2026-10-03 起所有笔记的向量索引
全部失败（日志里只有一句 WARNING），AI 问答桌面+手机双挂。

修复约定：vector_store.py 顶部显式 import 这两个子模块（纯 Python，无额外
依赖），PyInstaller 不加 hidden-import 也能收录；打包命令里再加
--collect-submodules 做第二道保险。
"""
import ast
import pathlib

VECTOR_STORE = (
    pathlib.Path(__file__).resolve().parents[1]
    / "app" / "services" / "vector_store.py"
)

# chromadb 配置字符串指向的动态加载模块（见 venv chromadb/config.py）
REQUIRED_LAZY_MODULES = {
    "chromadb.api.rust",
    "chromadb.telemetry.product.posthog",
}


def _static_imports(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                found.add(a.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            for a in node.names:
                found.add(f"{node.module}.{a.name}" if a.name != "*" else node.module)
    return found


def test_vector_store_statically_imports_chromadb_lazy_modules():
    imports = _static_imports(VECTOR_STORE)
    missing = {m for m in REQUIRED_LAZY_MODULES if m not in imports}
    assert not missing, (
        f"vector_store.py 缺少显式 import，打包后运行时动态加载会炸: {missing}"
    )
