
import json
import requests
import streamlit as st
from openai import OpenAI

# ---------------------------------------------------------
# 기본 설정
# ---------------------------------------------------------
st.set_page_config(page_title="에이블 날씨 비서", page_icon="🌤️")

# API 키: .streamlit/secrets.toml 에 OPENAI_API_KEY = "sk-..." 로 저장
client = OpenAI(api_key=st.secrets["OPENAI_API_KEY"])

# 역할 부여
sys_role = """
당신은 '김 대리'의 개인 비서 '에이블'입니다.
김 대리가 음성으로 물어본 지역의 날씨와 미세먼지 정보를 바탕으로
오늘 하루를 준비할 수 있도록 친절하고 간결하게 안내합니다.

답변은 다음 순서로 작성하세요.
1. 해당 지역의 날씨 요약 (기온, 체감온도, 강수 여부)
2. 미세먼지 상태 (PM10, PM2.5 수치와 등급)
3. 추천 의상
   - 반드시 [기온별 옷차림 가이드]에 있는 옷 중에서 추천하세요.
   - 현재 체감온도 기준 옷차림을 중심으로 안내하고,
     일교차가 크면 아침/저녁에 걸칠 겉옷도 함께 안내하세요.
   - 가이드에 팁이 있으면 꼭 함께 전달하세요.
4. 챙겨야 할 준비물 (비/눈 → 우산, 미세먼지 나쁨 이상 → 마스크,
   자외선이 강하거나 맑고 더운 날 → 선글라스·선크림, 일교차가 큰 날 → 겉옷 등)

주어진 데이터에 없는 수치는 지어내지 마세요.
음성으로 읽을 내용이므로 이모지, 특수기호, 마크다운 표기는 쓰지 말고 자연스러운 문장으로 작성하세요.

"""

# ---------------------------------------------------------
# 날씨 코드 → 한글 설명 (WMO 코드)
# ---------------------------------------------------------
WEATHER_CODE = {
    0: "맑음", 1: "대체로 맑음", 2: "구름 조금", 3: "흐림",
    45: "안개", 48: "짙은 안개",
    51: "약한 이슬비", 53: "이슬비", 55: "강한 이슬비",
    61: "약한 비", 63: "비", 65: "강한 비",
    71: "약한 눈", 73: "눈", 75: "강한 눈",
    80: "약한 소나기", 81: "소나기", 82: "강한 소나기",
    95: "뇌우", 96: "우박을 동반한 뇌우", 99: "강한 우박을 동반한 뇌우",
}


# ---------------------------------------------------------
# 기온별 옷차림 가이드 (첨부 이미지 기준)
# (최저 기온 기준, 옷 목록, 추가 팁) - 높은 온도 구간부터 순서대로
# ---------------------------------------------------------
CLOTHING_GUIDE = [
    (28, "민소매, 반팔, 반바지, 치마", ""),
    (23, "반팔, 얇은 셔츠, 반바지, 면바지", ""),
    (20, "얇은 가디건, 긴팔티, 면바지, 청바지", ""),
    (17, "얇은 니트, 가디건, 맨투맨, 얇은 자켓, 면바지, 청바지", ""),
    (12, "자켓, 가디건, 야상, 맨투맨, 니트, 스타킹, 청바지, 면바지", ""),
    (9,  "자켓, 트렌치코트, 야상, 니트, 스타킹, 청바지, 면바지", "아우터 안에 가디건을 꼭 입으세요"),
    (5,  "코트, 히트텍, 니트, 청바지, 레깅스", "레이어드(겹쳐 입기)가 필수예요"),
    (-100, "패딩, 두꺼운 코트, 목도리, 기모 제품", "최대한 많이 껴입으세요"),
]


def get_clothing(temp):
    """기온(℃)에 맞는 옷차림과 팁을 반환"""
    for min_temp, clothes, tip in CLOTHING_GUIDE:
        if temp >= min_temp:
            return clothes, tip


# ---------------------------------------------------------
# 미세먼지 등급 (한국 환경부 기준)
# ---------------------------------------------------------
def grade_pm10(v):
    if v <= 30: return "좋음"
    if v <= 80: return "보통"
    if v <= 150: return "나쁨"
    return "매우 나쁨"


def grade_pm25(v):
    if v <= 15: return "좋음"
    if v <= 35: return "보통"
    if v <= 75: return "나쁨"
    return "매우 나쁨"


# ---------------------------------------------------------
# 1) 음성 → 텍스트 (STT)
# ---------------------------------------------------------
def speech_to_text(audio_file):
    transcript = client.audio.transcriptions.create(
        model="whisper-1",
        file=("question.wav", audio_file.getvalue()),
        language="ko",
    )
    return transcript.text


# ---------------------------------------------------------
# 2) 질문에서 지역명 추출 (LLM)
# ---------------------------------------------------------
def extract_location(question):
    res = client.responses.create(
        model="gpt-4o-mini",
        input=[
            {"role": "system", "content":
                "사용자 질문에서 날씨를 묻는 지역명만 추출해 "
                'JSON으로만 답하세요. 예: {"location": "서울"} '
                '지역이 없으면 {"location": "서울"} 로 답하세요.'},
            {"role": "user", "content": question},
        ],
    )
    text = res.output_text.strip().replace("```json", "").replace("```", "")
    try:
        return json.loads(text)["location"]
    except Exception:
        return "서울"


# ---------------------------------------------------------
# 3) 지역명 → 위도/경도 (지오코딩)
# ---------------------------------------------------------
def geocode(location):
    url = "https://geocoding-api.open-meteo.com/v1/search"
    params = {"name": location, "count": 1, "language": "ko"}
    data = requests.get(url, params=params, timeout=10).json()
    if not data.get("results"):
        return None
    r = data["results"][0]
    return {"name": r["name"], "lat": r["latitude"], "lon": r["longitude"]}


# ---------------------------------------------------------
# 4) 날씨 + 미세먼지 조회
# ---------------------------------------------------------
def get_weather(lat, lon):
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat, "longitude": lon,
        "current": "temperature_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,uv_index_max",
        "timezone": "auto",
        "forecast_days": 1,
    }
    return requests.get(url, params=params, timeout=10).json()


def get_air_quality(lat, lon):
    url = "https://air-quality-api.open-meteo.com/v1/air-quality"
    params = {
        "latitude": lat, "longitude": lon,
        "current": "pm10,pm2_5",
        "timezone": "auto",
    }
    return requests.get(url, params=params, timeout=10).json()


def build_weather_summary(place, weather, air):
    cur, daily = weather["current"], weather["daily"]
    pm10, pm25 = air["current"]["pm10"], air["current"]["pm2_5"]

    feels = cur["apparent_temperature"]
    t_min = daily["temperature_2m_min"][0]
    t_max = daily["temperature_2m_max"][0]
    diff = round(t_max - t_min, 1)

    # 기온대별 옷차림 (체감 / 아침·저녁 / 낮)
    now_clothes, now_tip = get_clothing(feels)
    morning_clothes, morning_tip = get_clothing(t_min)
    day_clothes, _ = get_clothing(t_max)

    diff_note = ""
    if diff >= 10:
        diff_note = f"일교차가 {diff}도로 큽니다. 벗고 입기 편한 겉옷을 챙기세요."

    return f"""
[지역] {place}
[현재 날씨] {WEATHER_CODE.get(cur['weather_code'], '알 수 없음')}
[현재 기온] {cur['temperature_2m']}도 (체감 {feels}도)
[오늘 최저/최고 기온] {t_min}도 / {t_max}도 (일교차 {diff}도)
[강수 확률] {daily['precipitation_probability_max'][0]}%
[현재 강수량] {cur['precipitation']}mm
[풍속] {cur['wind_speed_10m']}km/h
[자외선 지수 최대] {daily['uv_index_max'][0]}
[미세먼지 PM10] {pm10}㎍/㎥ ({grade_pm10(pm10)})
[초미세먼지 PM2.5] {pm25}㎍/㎥ ({grade_pm25(pm25)})

[기온별 옷차림 가이드]
- 현재 체감온도({feels}도) 기준: {now_clothes} {('(' + now_tip + ')') if now_tip else ''}
- 아침/저녁 최저기온({t_min}도) 기준: {morning_clothes} {('(' + morning_tip + ')') if morning_tip else ''}
- 낮 최고기온({t_max}도) 기준: {day_clothes}
- 일교차 안내: {diff_note if diff_note else '일교차가 크지 않습니다.'}
"""
"""


# ---------------------------------------------------------
# 5) 답변 생성 (LLM)
# ---------------------------------------------------------
def make_answer(question, weather_summary):
    prompt = f"""
[김 대리의 질문]
{question}

[조회된 날씨 데이터]
{weather_summary}
"""
    res = client.responses.create(
        model="gpt-4o-mini",
        input=[{"role": "system", "content": sys_role},
               {"role": "user", "content": prompt}],
    )
    return res.output_text


# ---------------------------------------------------------
# 6) 텍스트 → 음성 (TTS)
# ---------------------------------------------------------
def text_to_speech(text):
    speech = client.audio.speech.create(
        model="gpt-4o-mini-tts",
        voice="nova",
        input=text,
        instructions="친절하고 또렷한 비서의 톤으로 읽어주세요",
    )
    return speech.content  # mp3 bytes


# ---------------------------------------------------------
# Streamlit 화면
# ---------------------------------------------------------
st.title("🌤️ 에이블 날씨 비서")
st.caption("마이크 버튼을 누르고 '오늘 서울의 날씨는 어때?'처럼 물어보세요.")

audio = st.audio_input("질문 녹음하기")

if audio is not None:
    # 같은 녹음을 중복 처리하지 않도록 확인
    audio_bytes = audio.getvalue()
    if st.session_state.get("last_audio") != audio_bytes:
        st.session_state["last_audio"] = audio_bytes

        with st.spinner("질문을 듣고 있어요..."):
            question = speech_to_text(audio)

        with st.spinner("날씨를 확인하고 있어요..."):
            location = extract_location(question)
            geo = geocode(location)

            if geo is None:
                answer = f"죄송해요, '{location}' 지역을 찾지 못했어요. 지역 이름을 다시 말씀해 주세요."
            else:
                weather = get_weather(geo["lat"], geo["lon"])
                air = get_air_quality(geo["lat"], geo["lon"])
                summary = build_weather_summary(geo["name"], weather, air)
                answer = make_answer(question, summary)

        with st.spinner("답변을 음성으로 만들고 있어요..."):
            mp3 = text_to_speech(answer)

        st.session_state["result"] = {
            "question": question, "answer": answer, "mp3": mp3,
        }

# 결과 출력 (텍스트 + 음성 동시)
if "result" in st.session_state:
    r = st.session_state["result"]
    st.markdown(f"**🎤 질문:** {r['question']}")
    st.markdown("---")
    st.markdown(r["answer"])
    st.audio(r["mp3"], format="audio/mp3", autoplay=True)
