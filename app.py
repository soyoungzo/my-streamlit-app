
import json
import time
import requests
import streamlit as st
from openai import OpenAI

# ---------------------------------------------------------
# Streamlit 화면
# ---------------------------------------------------------
st.title("🌤️ 에이블 날씨 비서")
st.caption("마이크 버튼을 누르고 '오늘 서울의 날씨는 어때?'처럼 물어보세요.")
client = OpenAI(api_key=st.secrets["OPENAI_API_KEY"], timeout=30.0, max_retries=1)

audio = st.audio_input("질문 녹음하기")

if audio is not None:
    # 같은 녹음을 중복 처리하지 않도록 확인
    audio_bytes = audio.getvalue()
    if st.session_state.get("last_audio") != audio_bytes:
        st.session_state["last_audio"] = audio_bytes
        st.session_state.pop("result", None)

        try:
            with st.status("에이블이 처리 중이에요...", expanded=True) as status:
                start = time.time()

                st.write("🎧 질문을 듣고 있어요...")
                question = speech_to_text(audio)
                st.write("✅ 질문 인식 완료 (" + str(round(time.time() - start, 1)) + "초)")

                st.write("🌍 지역과 날씨를 확인하고 있어요...")
                location = extract_location(question)

                if not location["korean"]:
                    # 질문에서 지역을 알아듣지 못한 경우
                    answer = "지역을 알아듣지 못했어요. 어느 지역의 날씨가 궁금한지 다시 말씀해 주세요."
                else:
                    geo = geocode(location)
                    if geo is None:
                        # 지역명은 알아들었지만 검색에서 찾지 못한 경우
                        answer = location["korean"] + " 지역은 찾지 못했어요. 지역 이름을 다시 말씀해 주세요."
                    else:
                        weather = get_weather(geo["lat"], geo["lon"])
                        air = get_air_quality(geo["lat"], geo["lon"])
                        summary = build_weather_summary(geo["name"], weather, air)
                        st.write("✅ 날씨 확인 완료 (" + str(round(time.time() - start, 1)) + "초)")
                        st.write("📝 답변을 작성하고 있어요...")
                        answer = make_answer(question, summary)

                st.write("🔊 음성을 만들고 있어요...")
                mp3 = text_to_speech(answer)
                st.write("✅ 모두 완료 (" + str(round(time.time() - start, 1)) + "초)")
                status.update(label="완료!", state="complete", expanded=False)

            st.session_state["result"] = {
                "question": question,
                "answer": answer,
                "mp3": mp3,
            }

        except Exception as e:
            st.error("처리 중 문제가 생겼어요. 다시 녹음해 주세요. (" + str(e) + ")")

# 결과 출력 (텍스트 + 음성 동시)
if "result" in st.session_state:
    r = st.session_state["result"]
    st.markdown("**🎤 질문:** " + r["question"])
    st.markdown("---")
    st.markdown(r["answer"])
    st.audio(r["mp3"], format="audio/mp3", autoplay=True)
