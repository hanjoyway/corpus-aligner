"""Central config. Reads .env if present (never commit .env)."""
from __future__ import annotations

import os
from pathlib import Path

# 桌面版绝不读 .env——打包时万一把开发机的 .env 带进去，别人一装就在用我的额度。
# 单机版的密钥一律来自用户目录下的 settings.json（见 desktop/settings.py）。
if os.getenv("DESKTOP_MODE") != "1":
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent / ".env")
    except ImportError:
        pass

ROOT = Path(__file__).resolve().parent
# 桌面版打包后程序目录是只读的临时解压目录，可写数据改放用户目录
# （由 desktop/settings.py 的 bootstrap 设好这个环境变量）。
DATA_DIR = Path(os.getenv("DESKTOP_DATA_DIR") or (ROOT / "data"))
INDEX_DIR = DATA_DIR / "index"

# --- embedding backend ----------------------------------------------------
# --- access gate (site not public yet) -----------------------------------
# If ACCESS_CODE is empty, the gate is disabled and the site is open.
ACCESS_CODE = os.getenv("ACCESS_CODE", "")
SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-change-me")
# 语料对齐工作台（/align）额外的第二道访问码，独立于站点门禁 ACCESS_CODE。
# 置空则不启用第二道锁（通过站点门禁即可直接用 /align，等同旧行为）。
ALIGN_ACCESS_CODE = os.getenv("ALIGN_ACCESS_CODE", "")
# 独立子域名：给只需要对齐工具、不需要检索功能的人用。这个域名下只露出 /align，
# 站点门禁 ACCESS_CODE 不适用（只认 ALIGN_ACCESS_CODE），其余路径一律不可达。
ALIGN_HOST = os.getenv("ALIGN_HOST", "align.ai4language.cn")
# 整体关闭语料加工工具（/align 及其 API）。用于只做检索的交付实例——对齐/OCR
# 属我方生产工具，不对客户开放；置 1 时相关路由一律 404，前端也不显示入口。
DISABLE_ALIGN = os.getenv("DISABLE_ALIGN", "") == "1"
# 单机桌面版（desktop/main.py 打包成 .app/.exe 双击运行）。只服务本机 127.0.0.1，
# 故整站门禁全部跳过；不加载语料库（桌面版只做加工，不做检索），根路径直接进
# 工作台。API key 由用户在应用内的「设置」页填写，写进用户目录而非程序包。
DESKTOP = os.getenv("DESKTOP_MODE", "") == "1"

# --- 语料防抓取（核心：保护语料资产）-------------------------------------
# 每个账号（未登录时按 IP）每天最多能取走的句对数。注意计的是**实际显示出来的
# 句对**（每页 20 条），不是命中总数——搜出 1 万条只翻两页，只算 40 条。
# 1 万条/天对人工使用绰绰有余（≈500 次检索），脚本要拖走全库仍需 20 天以上。
# 置 0 = 不限制。
DAILY_ROW_QUOTA = int(os.getenv("DAILY_ROW_QUOTA", "10000"))
# 单次检索最多能翻到第几条（防止「显示更多」被脚本一直翻到底把整库倒出来）。
MAX_SEARCH_OFFSET = int(os.getenv("MAX_SEARCH_OFFSET", "500"))

# --- 邮件（阿里云 DirectMail）：邮箱绑定 / 找回密码 ------------------------
# 凭据只写在服务器 .env（权限 600），绝不进 git。端口固定 465 + SSL。
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("SMTP_USER", "")          # 也是发件地址，必须已验证
SMTP_PASS = os.getenv("SMTP_PASS", "")
# 发件显示名，也用作邮件主题的落款。默认通用，交付实例在 .env 里设成自己的名字。
MAIL_FROM_NAME = os.getenv("MAIL_FROM_NAME", "双语语料库")
# DirectMail 免费额度 200 封/天且**多项目共用**（期刊追踪已占 190 上限），
# 故本项目自设小上限；本项目用量极低（绑定/找回各一封），30 封足够。
MAIL_DAILY_CAP = int(os.getenv("MAIL_DAILY_CAP", "30"))
# 找回密码链接有效期（分钟）
RESET_TOKEN_TTL_MIN = int(os.getenv("RESET_TOKEN_TTL_MIN", "30"))
# 站点对外地址（拼找回密码链接用）
SITE_URL = os.getenv("SITE_URL", "https://corpus.ai4language.cn")

SILICONFLOW_API_KEY = os.getenv("SILICONFLOW_API_KEY", "")
SILICONFLOW_BASE_URL = os.getenv(
    "SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1"
)
EMBED_MODEL = os.getenv("EMBED_MODEL", "BAAI/bge-m3")
EMBED_DIM = 1024  # bge-m3
# 语义检索「相关」阈值：余弦相似度 ≥ 此值才计入结果总数。
# bge-m3 分数基线偏高（无关句子也有 0.4 上下、中位数约 0.45），实测 0.6 区分度好：
# 无关查询只剩几十条，切题查询几百到几千条。调低会把半个库算成命中。
SEMANTIC_MIN_SCORE = float(os.getenv("SEMANTIC_MIN_SCORE", "0.6"))

# --- smart Q&A (RAG via DeepSeek) -----------------------------------------
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
# 辅助翻译提示词里的机构名。交付实例在 .env 里填自己的名字；
# 留空则用通用表述（打包成单机版发出去时不带任何客户信息）。
TRANSLATE_ORG = os.getenv("TRANSLATE_ORG", "")

QA_MODEL = os.getenv("QA_MODEL", "deepseek-chat")  # v4-flash, thinking off
QA_EVIDENCE_K = 10  # sentence pairs retrieved as grounding evidence

# --- OCR stage (scanned PDF -> text via 智谱 GLM-OCR) ----------------------
GLM_OCR_API_KEY = os.getenv("GLM_OCR_API_KEY", "")
GLM_OCR_URL = "https://open.bigmodel.cn/api/paas/v4/layout_parsing"
# 视觉模型兜底:layout_parsing 偶发 400 时,改用智谱 GLM-4V 视觉对话模型转写同一张图
# (同一个 key、另一个更稳的端点)。可用 env 换更强/更便宜的型号。
GLM_VLM_MODEL = os.getenv("GLM_VLM_MODEL", "glm-4v-flash")
# 跨厂商兜底:智谱(OCR + GLM-4V 同一套审核)若因敏感词被拦,改用千问 Qwen2.5-VL,
# 走 SiliconFlow(OpenAI 兼容,复用 embedding 的同一个 key),不同审核体系,绕开拦截。
QWEN_VL_MODEL = os.getenv("QWEN_VL_MODEL", "Qwen/Qwen2.5-VL-72B-Instruct")

# Corpora included in the semantic index for the prototype demo.
# Keyword search always covers everything; this is just the vector subset.
DEMO_CORPORA = [
    "2000-2019年政府工作报告",
    "中国共产党党章",
    "习近平谈治国理政一二三四卷",
    "2007-2019年达沃斯世界经济论坛语料库合集",
    "粤港澳大湾区发展规划纲要",
    "国民经济和社会发展十三五规划纲要",
    "一带一路中英双语数据库",
    "十九大、十八大、十七大、十六大、十五大报告",
]

# --- 业务类别（多条件检索的「按业务类别」维度）----------------------------
# 每个句对可归属一个「业务类别」。存量语料按下表归类；交付给中心后，把类别改成
# 中心自己的业务条线即可——入库时逐条打标（/api/align/ingest 的 category 参数）会
# 覆盖此表。不在表中的语料库，类别回退为语料库名本身，绝不丢句。
# 改这一处 = 改全站的业务类别口径。
CORPUS_CATEGORIES: dict[str, str] = {}   # 单机版不做检索，无业务类别


def category_of(corpus: str, explicit: str = "") -> str:
    """解析一个句对的业务类别：显式标注优先，其次查映射表，最后回退语料库名。"""
    return (explicit or "").strip() or CORPUS_CATEGORIES.get(corpus, corpus)


