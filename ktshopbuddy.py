import os
import json
from typing import Dict, Any, List
import re
import math
import pandas as pd
import streamlit as st
from openai import AzureOpenAI
from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
# from dotenv import load_dotenv
# load_dotenv()

# -------------------------
# Boot
# -------------------------
st.set_page_config(page_title="KTShop Buddy", page_icon="📱", layout="wide")

# -------------------------
# Azure Client
# -------------------------
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION")
AZURE_OPENAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT")
AZURE_AI_SEARCH_ENDPOINT = os.getenv("AZURE_SEARCH_ENDPOINT")
AZURE_AI_SEARCH_API_KEY = os.getenv("AZURE_SEARCH_KEY")
PLANS_INDEX = os.getenv("PLANS_INDEX")
DEVICES_INDEX = os.getenv("DEVICES_INDEX")

if not (AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY and AZURE_OPENAI_DEPLOYMENT):
    st.warning("환경 변수(ENDPOINT/API_KEY/DEPLOYMENT)가 설정되지 않았어요. .env를 확인해주세요.")

client = AzureOpenAI(
    api_key=AZURE_OPENAI_API_KEY,
    api_version=AZURE_OPENAI_API_VERSION,
    azure_endpoint=AZURE_OPENAI_ENDPOINT,
)

# 요금제 검색 
def get_plan_search_client() -> SearchClient:
    return SearchClient(
        endpoint=AZURE_AI_SEARCH_ENDPOINT,
        index_name=PLANS_INDEX,
        credential=AzureKeyCredential(AZURE_AI_SEARCH_API_KEY),
    )

# 단말 검색
def get_device_search_client() -> SearchClient:
    return SearchClient(
        endpoint=AZURE_AI_SEARCH_ENDPOINT,
        index_name=DEVICES_INDEX,
        credential=AzureKeyCredential(AZURE_AI_SEARCH_API_KEY),
    )


# -------------------------
# Sidebar: 사용자 조건 입력
# -------------------------
st.sidebar.header("🔎 검색 조건 입력")

# 데이터 무제한 선태 영역 추가; 무제한인 경우 데이터 수치 미사용
data_unlimited = st.sidebar.checkbox("데이터 무제한", value=False)
if data_unlimited:
    data_gb = None
else:
    data_gb = st.sidebar.slider("📡 월 데이터 사용량 (GB)", 1, 150, 50, step=1)

voice_choice = st.sidebar.selectbox("📞 통화", ["60분", "120분", "300분", "무제한"], index=3)
budget = st.sidebar.number_input("💰 요금제 예산 (월기준/원)", min_value=10000, max_value=200000, value=90000, step=1000)
brand_pref = st.sidebar.multiselect("📱 선호 휴대폰 브랜드", ["Samsung", "Apple", "Xiaomi"], default=["Samsung"])
device_budget = st.sidebar.number_input("💸 희망 단말 예산 (일시불 기준/원)", min_value=100000, max_value=3500000, value=1500000, step=100000)
installment_months = st.sidebar.selectbox("😇 희망 단말대금 할부 개월수", options=[0, 12, 24], index=1)
notes = st.sidebar.text_area("기타 요구사항 (예: 멤버십 VIP 혜택, 가벼운 휴대폰 등)", "")


# -------------------------
# Main Layout
# -------------------------
st.title("˖⁺‧₊˚ 🕵🏻 KTShop Buddy ˚₊‧⁺˖")
st.caption("왼쪽 폼을 입력하여 나에게 딱 맞는 KT 요금제와 단말을 찾아보세요! ƪ(˘⌣˘)ʃ")

# 대화 히스토리 초기화 및 기본 설정
if "messages" not in st.session_state:
    st.session_state.messages = [
        {
            "role": "system",
            "content": (
                "너는 KT의 요금제/단말 추천 어시스턴트다. "
                "사용자 조건(데이터/통화/예산/브랜드/기타)을 분석하고, "
                "지나치게 확신하지 말고 근거 중심으로 간결히 설명한다. "
                "반드시 JSON도 함께 출력한다."
            ),
        }
    ]


# -------------------------
# Util: 공통
# -------------------------
NUM_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?")
def to_float_safe(x: Any) -> float | None:
    """
    문자열에서 숫자 찾아서 실수형으로 변환하는 함수
    """
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).replace(",", "")
    m = NUM_RE.search(s)
    if not m:
        return None
    try:
        return float(m.group())
    except:
        return None

def format_currency(value: Any, show_unit: bool = True, decimals: int = 0, dash: str = "-") -> str:
    """
    금액/숫자 문자열을 안전하게 포맷팅: 1200000 -> '1,200,000원'
    - 콤마/한글/기호 섞여도 숫자만 추출(to_float_safe 활용 가정)
    - None이나 파싱 실패 시 dash 반환
    - decimals로 소수 표시 자리수 제어(기본 0)
    """
    num = to_float_safe(value)
    if num is None:
        return dash
    if decimals <= 0:
        s = f"{int(round(num)):,}"
    else:
        s = f"{num:,.{decimals}f}"
    return f"{s}" if show_unit else s

# -------------------------
# Util: 요금제 관련 함수
# -------------------------
def score_plan(doc: Dict[str, Any], target_gb: float | None, target_price: float | None, want_unlimited: bool) -> float:
    """
    사용자 조건과 비교하여 점수 매기는 함수. 요금제별 score가 낮을수록 조건에 적합
    무제한 요청이면 '무제한' 문자열 매칭을 최우선 가점.
    아니면 기존 데이터/가격 편차 가중치(0.6/0.4).
    """
    # 데이터 중요도는 60%, 가격은 중요도 40%. 요금제 금액 숫자화 적용
    w_gb, w_price = 0.6, 0.4
    price = to_float_safe(doc.get("monthly_fee"))

    # 요금제가 무제한인지 판단
    data_field = (doc.get("data_gb") or "").lower()
    is_unlimited_doc = any(k in data_field for k in ["무제한", "unlimited", "완전무제한"])

    # 고객은 무제한 요금제를 원하나, 요금제에 무제한 문구가 없으면 추천 대상에서 제외. 그 외의 경우 요금제 금액 차이만 비교하여 계산
    if want_unlimited:
        if not is_unlimited_doc:
            return math.inf
        price_gap = abs((price or 0) - (target_price or 0)) if (price is not None and target_price is not None) else 10000.0
        return w_price * (price_gap / 10000.0)
    else:
        # 데이터 숫자화 적용
        gb = to_float_safe(doc.get("data_gb"))
        # 사용자 조건과 비교하여 차이 계산. 데이터나 가격 정보가 없는 경우 디폴트 패널티(5GB/10000원 차이) 부여
        gb_gap = abs((gb or 0) - (target_gb or 0)) if (gb is not None and target_gb is not None) else 5.0
        price_gap = abs((price or 0) - (target_price or 0)) if (price is not None and target_price is not None) else 10000.0
        # 데이터, 가격 비교 최종 점수 계산
        return w_gb * gb_gap + w_price * (price_gap / 10000.0)


def fetch_plan_candidates(data_gb: int | None, budget: int, data_unlimited: bool, topn) -> List[Dict[str, Any]]:
    """
    keyword search) 사용자 조건에 맞는 요금제 후보를 가져와 점수로 정렬 후 상위 N개를 반환하는 함수
    사용자가 무제한 요금제를 원하는 경우면 무제한 키워드 위주로, 아닌 경우 GB/예산 키워드 혼합.
    """
    # Azure Search ai 연결
    sc = get_plan_search_client()

    # 고객이 무제한을 원하는 경우 '무제한' 단어 중심으로 검색, 그 외의 경우 사용자가 원하는 조건으로 keyword 설정
    if data_unlimited:
        query_terms = ["무제한", "데이터 무제한", "unlimited", "완전무제한", "요금제"]
    else:
        query_terms = [
            f"{data_gb}GB", f"{data_gb} 기가",
            f"{int(budget/10000)}만원", f"{budget}원",
            "요금제", "데이터"
        ]
    # keyword가 하나라도 포함된 문서 찾기위한 쿼리문
    search_text = " OR ".join([str(t) for t in query_terms if t])

    # Azure search ai 인덱스에서 검색 수행. 검색 결과 최대 50개만 찾기.
    results = sc.search(
        search_text=search_text,
        top=50,
        include_total_count=False,
        query_type="simple",
        select=[
            "planId","plan_name","network","monthly_fee","data_gb","voice",
            "throttling","roaming","membership","message","benefit_1","benefit_2"
        ],
    )
    # 검색 결과를 딕셔너리 리스트로 변환
    docs: List[Dict[str, Any]] = [dict(r) for r in results]
    # 요금제 점수 계산 및 오름차순 정렬
    scored = [(score_plan(d, float(data_gb) if data_gb is not None else None, float(budget), data_unlimited), d) for d in docs]
    scored.sort(key=lambda x: x[0])
    topk = [d for _, d in scored[:topn]]
    return topk


def compact_plan_json(docs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    검색된 요금제 리스트 중 필요한 항목만 추출하여 모델 프롬프트에 전달하거나 표로 노출하기 위함
    """
    out = []
    for d in docs:
        out.append({
            "번호": d.get("planId"),
            "요금제": d.get("plan_name"),
            "요금(월)": format_currency(d.get("monthly_fee")),
            "데이터(GB)": d.get("data_gb"),
            "데이터 초과시": d.get("throttling"),
            "전화": d.get("voice"),
            "멤버십": d.get("membership"),
            "혜택1": d.get("benefit_1"),
            "혜택2": d.get("benefit_2"),
        })
    return out


# -------------------------
# Util: 단말 관련 함수
# -------------------------
def score_device(doc: Dict[str, Any], target_price: float | None, brand_pref: List[str]) -> float:
    """
    사용자 조건과 비교하여 점수 매기는 함수. 단말별 score가 낮을수록 조건에 적합.
    가격 차이를 기본으로 하며, 브랜드 선호 매칭여부에 따라 보너스 점수 부여
    """
    # 가격 차이 계산. 가격 정보가 없는 경우 후보에서 제외될 수 있도록 큰 값 부여.
    price = to_float_safe(doc.get("price"))
    price_gap = abs((price or 0) - (target_price or 0)) if (price is not None and target_price is not None) else 5e5

    # 선호 브랜드 매칭 점수 계산
    bonus = 0.0
    brand = (doc.get("brand") or "").lower()
    if brand_pref:
        if brand in [b.lower() for b in brand_pref]:
            bonus -= 0.5  # 가벼운 가점

    # 가격 갭을 만원 단위로 대략 정규화 + 보너스 반영
    return (price_gap / 10000.0) + bonus


def dedupe_devices_by_model_storage(docs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    동일 모델과 동일 용량인 경우, sntyNo 기준으로 값이 가장 작은 단말 1개만 유지하여 중복 제거
    """
    # 용량 표기 통일화. '256gb' '256 GB' 등 숫자만 추출해 통일
    def normalize_storage(x: Any) -> str:
        s = str(x or "").strip().lower().replace(" ", "")
        m = NUM_RE.search(s)
        return m.group() + "gb" if m else s

    # 중복 그룹별로 가장 좋은 단말만 저장할 딕셔너리 초기화
    best: Dict[tuple, Dict[str, Any]] = {}
    # 각 단말을 (모델,용량) 단위로 묶기 위한 그룹 키 생성
    for d in docs:
        key = ((d.get("model") or "").strip().lower(), normalize_storage(d.get("storage_gb")))
        # sntyNo 숫자 추출. 값이 없으면 큰 수 부여하여 최하위 순위 부여
        snty_raw = d.get("sntyNo")
        try:
            snty_val = int(NUM_RE.search(str(snty_raw)).group()) if snty_raw and NUM_RE.search(str(snty_raw)) else 10**12
        except:
            snty_val = 10**12

        # 처음보는 (모델,용량)이면 바로 딕셔너리 등록. 동일 조합이 있는 경우 sntyNo 값이 작은 쪽으로 변경.
        if key not in best:
            best[key] = d
            best[key]["__snty_val"] = snty_val
        else:
            if snty_val < best[key]["__snty_val"]:
                d["__snty_val"] = snty_val
                best[key] = d
    # 정리 후 반환
    out = []
    for d in best.values():
        d.pop("__snty_val", None)
        out.append(d)
    return out


def fetch_device_candidates(device_budget: int, brand_pref: List[str], topn) -> List[Dict[str, Any]]:
    """
    keyword search) 사용자 조건에 맞는 단말 후보를 가져와 점수로 정렬 후 상위 N개를 반환하는 함수.
    예산과 브랜드 선호도를 바탕으로 중복 제거 후 스코어링
    """
    # Azure Search ai 연결
    sc = get_device_search_client()

    # keyword가 하나라도 포함된 문서 찾기위한 쿼리문
    query_terms = []
    if brand_pref and len(brand_pref)>0: 
        query_terms.extend(brand_pref)
    else: query_terms.extend(["Samsung", "Apple", "Xiaomi"])
    query_terms.extend([f"{device_budget}원", f"{int(device_budget/10000)}만원", "스마트폰", "휴대폰", "폰"])
    search_text = " OR ".join([str(t) for t in query_terms if t])

    # Azure search ai 인덱스에서 검색 수행. 검색 결과 최대 50개만 찾기.
    results = sc.search(
        search_text=search_text if search_text else "*",
        top=50,
        include_total_count=False,
        query_type="simple",
        select=["prodNo","sntyNo","brand","model","storage_gb","color","price","weight_g","display_size_cm"],
    )
    docs: List[Dict[str, Any]] = [dict(r) for r in results]

    # 동일 모델 및 용량 중복 제거
    docs = dedupe_devices_by_model_storage(docs)
    # 단말 점수 계산 및 오름차순 정렬
    scored = [
        (score_device(d, float(device_budget), brand_pref), d)
        for d in docs
    ]
    scored.sort(key=lambda x: x[0])
    topk = []
    for s, d in scored[:topn]:
        d = dict(d)
        d["__score"] = s  
        topk.append(d)
    return topk


def compact_device_json(docs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    검색된 단말 리스트 중 필요한 항목만 추출하여 모델 프롬프트에 전달하거나 표로 노출하기 위함
    """
    rows = []
    for d in docs:
        rows.append({
            "브랜드": d.get("brand"),
            "모델": d.get("model"),
            "용량(GB)": d.get("storage_gb"),
            "색상": d.get("color"),
            "가격(원)": format_currency(d.get("price")),
            "무게(g)": d.get("weight_g"),
            "디스플레이(cm)": d.get("display_size_cm"),
        })
    return rows


# -------------------------
# Prompt: ai 모델에게 적용할 프롬프트
# -------------------------
def build_plan_prompt(plan_candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    사용자 입력과 요금제 후보를 정리하여 LLM에 전달할 프롬프트
    """
    # 사용자 조건 정의
    prefs = {
        "data_gb": data_gb,
        "voice": voice_choice,
        "budget": budget,
        "notes": notes,
    }
    # LLM이 참고할 요금제를 컨텍스트로 제공
    plan_ctx = {
        "plan_candidates": compact_plan_json(plan_candidates)
    }

    # 사용자 요청 프롬프트
    user_text = (
        "다음 사용자 조건에 맞춰 **KT 요금제** TOP3를 추천해줘. "
        "주어진 plan_candidates 중에서만 선택하고, 가정은 최소화해.\n\n"
        f"조건(JSON): ```json\n{json.dumps(prefs, ensure_ascii=False)}\n```\n"
        f"plan_candidates(JSON): ```json\n{json.dumps(plan_ctx, ensure_ascii=False)}\n```\n\n"
        "반드시 아래 JSON 스키마를 포함한 결과를 생성하고, 근거가 되는 필드(월정액/데이터/음성 등)를 간단히 설명해줘.\n"
        "```json\n"
        "{\n"
        '  "recommendations": [\n'
        "    {\n"
        '      "rank": 1,\n'
        '      "plan": {"planId": "0946", "name": "요금제명", "monthly_fee": 69000, "data_gb": "무제한 또는 수치", "voice": "무제한 또는 분수"},\n'
        '      "monthly_total": 69000,\n'
        '      "tco": 69000,\n'
        '      "reasons": ["이유1", "이유2"],\n'
        '      "caveats": ["주의1"]\n'
        "    }\n"
        "  ],\n"
        '  "alternatives": ["대안1", "대안2"]\n'
        "}\n"
        "```\n"
        "주의: 가격 등 숫자는 후보의 값을 그대로 사용하고, 후보에 없는 정보는 임의로 만들지 마 "
        "사용자가 데이터 무제한을 원하면 무제한 요금제를 우선 추천해."
        "사용자가 이해하기 쉽게 설명해줘"
    )
    # streamlit 세션 메세지에 추가
    msgs: List[Dict[str, Any]] = list(st.session_state.messages) + [{"role": "user", "content": user_text}]
    return msgs


def build_device_prompt(device_candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    사용자 입력과 단말 후보를 정리하여 LLM에 전달할 프롬프트
    """
    # 사용자 조건 정의
    prefs = {
        "device_budget": device_budget,
        "brand_pref": brand_pref,
        "notes": notes,
    }
    # LLM이 참고할 단말을 컨텍스트로 제공
    device_ctx = {
        "device_candidates": [
            {
                "prodNo": d.get("prodNo"),
                "sntyNo": d.get("sntyNo"),
                "brand": d.get("brand"),
                "model": d.get("model"),
                "storage_gb": d.get("storage_gb"),
                "color": d.get("color"),
                "price": d.get("price"),
                "weight_g": d.get("weight_g"),
                "display_size_cm": d.get("display_size_cm"),
            }
            for d in device_candidates
        ]
    }

    user_text = (
        "다음 사용자 조건에 맞춰 **단말(스마트폰)** Top3를 추천해줘. "
        "반드시 주어진 device_candidates **내에서만 선택**하고, 임의로 새로운 정보를 만들지 마.\n\n"
        f"조건(JSON): ```json\n{json.dumps(prefs, ensure_ascii=False)}\n```\n"
        f"device_candidates(JSON): ```json\n{json.dumps(device_ctx, ensure_ascii=False)}\n```\n\n"
        "아래 JSON 스키마로 결과를 생성하고, 선택 근거와 유의사항을 간단히 설명해줘.\n"
        "```json\n"
        "{\n"
        '  "recommendations": [\n'
        "    {\n"
        '      "rank": 1,\n'
        '      "device": {\n'
        '        "prodNo": "string", "sntyNo": "string",\n'
        '        "brand": "Samsung", "model": "Galaxy S24", "storage_gb": "256", "color": "Green",\n'
        '        "price": "1250000", "weight_g": "167", "display_size_cm": "15.7"\n'
        "      },\n"
        '      "reasons": ["예산과의 적합성", "브랜드/모델 선호 일치", "용량/무게/디스플레이 균형"],\n'
        '      "caveats": ["가격 변동 가능성"]\n'
        "    }\n"
        "  ],\n"
        '  "alternatives": ["대안1 간단 사유", "대안2 간단 사유"]\n'
        "}\n"
        "```\n"
        "주의: 가격 등 숫자는 후보의 값을 그대로 사용하고, 후보에 없는 값은 추정하지 마."
        "사용자가 이해하기 쉽게 설명해줘"
    )
    msgs: List[Dict[str, Any]] = list(st.session_state.messages) + [{"role": "user", "content": user_text}]
    return msgs


# -------------------------
# Util: LLM 결과를 테이블로 변환
# -------------------------
def to_device_rows_from_llm(rec_json: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = []
    for item in rec_json.get("recommendations", []):
        d = item.get("device", {})
        rows.append(
            {
                "순위": item.get("rank"),
                "브랜드": d.get("brand"),
                "모델": d.get("model"),
                "용량(GB)": d.get("storage_gb"),
                "색상": d.get("color"),
                "가격(원)": format_currency(d.get("price")),
                "이유": ". ".join(item.get("reasons", [])),
                "주의": ". ".join(item.get("caveats", [])),
            }
        )
    return rows

def to_plan_rows_from_llm(rec_json: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = []
    for item in rec_json.get("recommendations", []):
        plan = item.get("plan", {})
        rows.append(
            {
                "순위": item.get("rank"),
                "요금제": plan.get("name"),
                "요금(월)": format_currency(plan.get("monthly_fee")),
                "데이터(GB)": plan.get("data_gb"),
                "통화": plan.get("voice"),
                "이유": ". ".join(item.get("reasons", [])),
                "주의": ". ".join(item.get("caveats", [])),
            }
        )
    return rows

# 상위 3개 결과만 적재
def extract_top_devices_from_llm(parsed_device: Dict[str, Any], k: int = 3) -> List[Dict[str, Any]]:
    out = []
    for item in (parsed_device or {}).get("recommendations", [])[:k]:
        d = item.get("device", {})
        out.append({
            "prodNo": d.get("prodNo"),
            "sntyNo": d.get("sntyNo"),
            "brand": d.get("brand"),
            "model": d.get("model"),
            "storage_gb": d.get("storage_gb"),
            "color": d.get("color"),
            "price": to_float_safe(d.get("price")),
            "weight_g": d.get("weight_g"),
            "display_size_cm": d.get("display_size_cm"),
            "reasons": item.get("reasons", []),
            "caveats": item.get("caveats", []),
        })
    return [x for x in out if x.get("price") is not None]

def extract_top_plans_from_llm(parsed_plan: Dict[str, Any], k: int = 3) -> List[Dict[str, Any]]:
    out = []
    for item in (parsed_plan or {}).get("recommendations", [])[:k]:
        p = item.get("plan", {})
        out.append({
            "planId": p.get("planId"),
            "name": p.get("name"),
            "monthly_fee": to_float_safe(p.get("monthly_fee")),
            "data_gb": p.get("data_gb"),
            "voice": p.get("voice"),
            "reasons": item.get("reasons", []),
            "caveats": item.get("caveats", []),
        })
    return [x for x in out if x.get("monthly_fee") is not None]


# -------------------------
# Util: JSON 파싱 + 테이블 준비
# -------------------------
def safe_parse_json(txt: str) -> Dict[str, Any]:
    """
    LLM이 생성한 응답 문자열에서 json 데이터 추출
    """
    candidates = re.findall(r"```json\s*(.*?)\s*```", txt, flags=re.DOTALL | re.IGNORECASE)
    # LLM 모델이 json 을 여러번 출력하거나 불완전한 json을 만드는 경우를 대비하여 가장 마지막 json 파싱
    if candidates:
        for c in reversed(candidates):
            try:
                return json.loads(c)
            except Exception:
                continue
    # 코드블록이 없는 경우 전체에서 직접 파싱 시도
    try:
        return json.loads(txt)
    except Exception:
        return {}


# -------------------------
# Util: 요금제 + 단말 결합 상품 계산 함수
# -------------------------
def build_combinations(plans: List[Dict[str, Any]], devices: List[Dict[str, Any]], months: int) -> List[Dict[str, Any]]:
    """
    요금제 후보, 단말 후보, 약정개월수 정보를 토대로 모든 조합으로 매칭하여 각 조합별 월 납부금액, 총비용 계산하는 함수
    """
    combos = []
    # 요금제와 단말을 모든 조합으로 매칭
    for p in plans:
        for d in devices:
            plan_fee = p["monthly_fee"] or 0.0
            device_price = d["price"] or 0.0

            # 단말 월 할부금 계산 : 원리금 균등상환 방식, 이자율 연 5.9%
            monthly_rate = 5.9 / 100 / 12
            monthly_payment = device_price * (monthly_rate * (1 + monthly_rate)**months) / ((1 + monthly_rate)**months - 1)
            monthly_device = math.floor(monthly_payment)

            monthly_total = plan_fee + monthly_device
            tco = (plan_fee * (months if months > 0 else 1)) + device_price

            combos.append({
                "plan": p,
                "device": d,
                "assumption_months": months,
                "monthly_device_payment": monthly_device,
                "monthly_total": monthly_total,
                "tco": tco,
            })
    # 비용 기준 오름차순
    combos.sort(key=lambda x: x["monthly_total"])
    return combos


def combo_rows(combos: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    요금제+단말 추천 조합 리스트 표기위한 함수
    """
    rows = []
    for i, c in enumerate(combos, 1):
        p, d = c["plan"], c["device"]
        plan_id = p.get("planId") or ""
        prod_no = d.get("prodNo") or ""
        snty_no = d.get("sntyNo") or ""
        # KT샵 구매 링크 생성
        buy_url = (
            f"https://shop.kt.com/mobile/view.do?prodNo={prod_no}&sntyNo={snty_no}&pplId={plan_id}"
            if plan_id and prod_no and snty_no else ""
        )
        rows.append({
            "순위": i,
            "요금제": p.get("name"),
            "요금제 월정액": format_currency(int(round(p.get("monthly_fee") or 0))),
            "단말": f"{d.get('brand','')} {d.get('model','')} {d.get('storage_gb','')}".strip(),
            "단말가(일시불)": format_currency(int(round(d.get("price") or 0))),
            "할부개월": c["assumption_months"],
            "단말 월납부금액": format_currency(int(round(c["monthly_device_payment"] or 0))),
            "월 총 납부금액": format_currency(int(round(c["monthly_total"] or 0))),
            "총 비용": format_currency(int(round(c["tco"] or 0))),
            "구매 링크": buy_url,
        })
    return rows


# -------------------------
# Action
# -------------------------
run = st.button("찾아보기 🔍")
st.markdown("---")

if run:

    # ----- 요금제 후보 조회 -----
    with st.spinner("버디가 최적의 요금제 찾는 중...(●'◡'●)"):
        try:
            plan_candidates = fetch_plan_candidates(
                data_gb=data_gb,
                budget=budget,
                data_unlimited=data_unlimited,
                topn=10,
            )
        except Exception as e:
            st.error(f"Azure Search 오류: {e}")
            plan_candidates = []
    
    # 요금제 Search 결과 보기
    if plan_candidates:
        st.subheader("📞 검색된 요금제 10가지")
        st.write("🕵🏻 입력한 조건을 바탕으로 버디가 찾은 10가지 요금제 정보입니다. 어떠신가요?")
        top10_by_score = sorted(plan_candidates, key=lambda d: d.get("__score", 0))[:10]
        st.dataframe(pd.DataFrame(compact_plan_json(top10_by_score)), use_container_width=True, hide_index=True,)
    else:
        st.warning("후보를 찾지 못했어요. 데이터/예산 슬라이더를 조정해 다시 시도해보세요.")
    st.markdown("---")

    # ----- 요금제 Top3 LLM 추천 -----
    with st.spinner("버디가 추천 요금제 Top3 선별 중...(●'◡'●)"):
        msgs = build_plan_prompt(plan_candidates)
        try:
            completion = client.chat.completions.create(
                model=AZURE_OPENAI_DEPLOYMENT,
                messages=msgs,
                temperature=0.4,
                max_tokens=2000,
            )
            reply = completion.choices[0].message.content or ""
        except Exception as e:
            st.error(f"OpenAI 호출 오류: {e}")
            reply = ""

    parsed = safe_parse_json(reply)
    if not parsed:
        st.warning("JSON을 파싱하지 못했어요. 입력을 바꾸거나 다시 실행해보세요.")
    else:
        rows = to_plan_rows_from_llm(parsed)
        if rows:
            df = pd.DataFrame(rows)[:3]
            st.subheader("🔝 추천 요금제 Top3")
            st.write("🕵🏻 가장 좋은 요금제 3종을 추천합니다! 버디가 선택한 이유랑 주의점 꼭 읽어보세요.")
            st.dataframe(df, use_container_width=True, hide_index=True,)
        else:
            st.info("추천 결과가 비어있어요. 입력 조건을 조정해 다시 시도해보세요.")
        with st.expander("🔎 버디의 생각 살펴보기", expanded=False):
            cleaned_reply = re.sub(r"```json.*?```", "", reply, flags=re.DOTALL | re.IGNORECASE).strip()
            st.markdown(cleaned_reply)
        alts = parsed.get("alternatives", [])
        if alts:
            st.write("🕵🏻 버디의 소곤소곤 대안책 한마디")
            for i, a in enumerate(alts[:3], 1):
                st.write(f"{i}. {a}")
    st.markdown("---")


    # ----- 단말 후보 조회 -----
    with st.spinner("버디가 최적의 단말 찾는 중...(●'◡'●)"):
        try:
            device_candidates = fetch_device_candidates(
                device_budget=device_budget,
                brand_pref=brand_pref,
                topn=10,
            )
        except Exception as e:
            st.error(f"Azure Search(Devices) 오류: {e}")
            device_candidates = []

    # 단말 Search 결과 보기
    if device_candidates:
        st.subheader("📱 검색된 단말 10가지")
        st.write("🕵🏻 입력한 조건을 바탕으로 버디가 찾은 10가지 단말 정보입니다. 이중에 마음에 드는 게 있나요?")
        top10_by_score = sorted(device_candidates, key=lambda d: d.get("__score", 0))[:10]
        st.dataframe(pd.DataFrame(compact_device_json(top10_by_score)), use_container_width=True, hide_index=True,)
    else:
        st.warning("단말 후보를 찾지 못했어요. 예산/브랜드/모델 키워드를 조정해보세요.")
    st.markdown("---")

    # ----- 단말 Top3 LLM 추천 -----
    if device_candidates:
        with st.spinner("버디가 추천 단말 Top3 선별 중...(●'◡'●)"):
            device_msgs = build_device_prompt(device_candidates)
            try:
                completion_device = client.chat.completions.create(
                    model=AZURE_OPENAI_DEPLOYMENT,
                    messages=device_msgs,
                    temperature=0.4,
                    max_tokens=2000,
                )
                reply_device = completion_device.choices[0].message.content or ""
            except Exception as e:
                st.error(f"OpenAI(Devices) 호출 오류: {e}")
                reply_device = ""

        parsed_device = safe_parse_json(reply_device)
        if not parsed_device:
            st.warning("단말 LLM JSON을 파싱하지 못했어요. 입력을 바꾸거나 다시 실행해보세요.")
        else:
            dev_rows = to_device_rows_from_llm(parsed_device)
            if dev_rows:
                st.subheader("🔝 추천 단말 Top3")
                st.write("🕵🏻 가장 좋은 단말 3종을 추천합니다! 버디가 선택한 이유랑 주의점 읽어보시죠.")
                df = pd.DataFrame(dev_rows)[:3]
                st.dataframe(df, use_container_width=True, hide_index=True)
            else:
                st.info("단말 LLM 추천 결과가 비어있어요.")
            
            with st.expander("🔎 버디의 생각 살펴보기", expanded=False):
                cleaned_reply = re.sub(r"```json.*?```", "", reply_device, flags=re.DOTALL | re.IGNORECASE).strip()
                st.markdown(cleaned_reply)

            dev_alts = parsed_device.get("alternatives", [])
            if dev_alts:
                st.write("🕵🏻 버디의 소곤소곤 대안책 한마디")
                for i, a in enumerate(dev_alts[:3], 1):
                    st.write(f"{i}. {a}")
    st.markdown("---")

    # ----- 요금제+단말 조합 Top3 LLM 추천 -----
    if device_candidates and parsed_device and parsed:
        try:
            top_plans = extract_top_plans_from_llm(parsed, k=3)
            top_devices = extract_top_devices_from_llm(parsed_device, k=3)

            if top_plans and top_devices:
                combos = build_combinations(top_plans, top_devices, months=installment_months)
                combo_top3_df = pd.DataFrame(combo_rows(combos[:3]))
                st.subheader("🏆 버디's pick : 요금제+단말 조합 BEST 3")
                st.write("🕵🏻 버디가 추천한 요금제와 단말로 총 비용이 저렴한 순으로 조합하였습니다. 아래 조합으로 KT샵에서 구매 가능합니다.")
                st.dataframe(
                    combo_top3_df,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "구매 링크": st.column_config.LinkColumn("구매 링크", display_text="KT Shop 바로가기")
                    },
                )
                st.write("다른 기준으로 요금제와 단말을 찾고 싶다면, 다시 검색해주세요!")
            else:
                st.info("조합을 만들 수 있을 만큼의 LLM Top3 결과가 부족합니다. (요금제/단말 모두 필요)")
        except Exception as e:
            st.error(f"조합 생성 중 오류: {e}")

    st.markdown("---")
    st.session_state.messages.append({"role": "user", "content": "(실행) 조건 기반 요금제+단말 LLM 추천"})
    st.session_state.messages.append({"role": "assistant", "content": reply})


# -------------------------
# 페이지 하단 KT샵 버디 유의사항 안내
# -------------------------
with st.expander("💙 KT샵 버디 유의사항 안내"):
    st.markdown(
        """
1) [요금제+단말 조합 추천] **단말 월납부 금액**의 경우 `공통지원금`, `KT샵 지원금` 등은 반영되어 있지 않습니다.

   단말 출고가 기준으로 계산되었으며, 월 할부금에 `할부 수수료 5.9%`가 포함된 금액입니다. 
2) 정확한 할인 지원금은 KT샵 홈페이지를 참고하세요.   🔗 [KT Shop 바로가기](https://shop.kt.com/smart/supportAmtList.do) 
        """
    )
