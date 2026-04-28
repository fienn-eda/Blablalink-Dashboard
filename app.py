import streamlit as st
import pandas as pd
from supabase import create_client, Client
from datetime import datetime, timedelta
from google import genai
import re
from collections import Counter
import plotly.express as px
import plotly.graph_objects as go

# ==========================================
#  1. 기본 설정 및 DB 연결
# ==========================================
st.set_page_config(page_title="Blablalink 인사이트 대시보드", page_icon="💬", layout="wide")

SUPABASE_URL = st.secrets["SUPABASE_URL"]
SUPABASE_KEY = st.secrets["SUPABASE_KEY"]
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ==========================================
#  2. 함수 모은거
# ==========================================
@st.cache_data(ttl=3600)
def load_dashboard_data():
    # 분석을 위해 넉넉히 최근 45일치 로드 (이전 업데이트 비교용)
    threshold_ts = int((datetime.now() - timedelta(days=45)).timestamp())
    # res = supabase.table("posts").select("*").gte("created_at", threshold_ts).execute()
    # 시간 필터를 빼고, 혹시 모를 과부하를 막기 위해 최신 3000개만.
    res = supabase.table("posts").select("*").order("created_at", desc=True).limit(3000).execute()
    df = pd.DataFrame(res.data)

    if not df.empty:
        # 시간 변환 (초 단위 유닉스 타임스탬프 기준)
        df['created_at_dt'] = pd.to_datetime(df['created_at'], unit='s', utc=True).dt.tz_convert('Asia/Seoul')
        df['date'] = df['created_at_dt'].dt.date
    return df

# ==========================================
# 2-1. 데이터 로드 및 마스터 방어막 (Early Exit)
# ==========================================
df_all = load_dashboard_data()

# 💡 데이터가 없거나 권한(RLS) 문제로 막힌 경우, 여기서 대시보드 렌더링을 완전히 멈춥니다.
if df_all.empty or 'plate_name' not in df_all.columns:
    st.error("🚨 데이터베이스에서 데이터를 불러오지 못했습니다.")
    st.info("💡 해결 방법: Supabase 대시보드 -> Authentication -> Policies에서 'posts' 테이블의 RLS를 해제(Disable)해 주세요.")
    st.stop() # 🛑 아래에 있는 함수나 UI 코드는 아예 실행되지 않음!

# ==========================================
# 2-2. 유틸리티 함수 모음
# ==========================================
# 댓글의 노이즈를 필터링하는 도우미 함수
def is_valid_comment(raw_text):
    if not isinstance(raw_text, str): return False

    # HTML 태그 제거
    pure_text = re.sub(r'<[^>]+>', '', raw_text).strip()

    # 2글자 이하 필터링
    if len(pure_text) <= 2: return False

    # 한/영/숫자/일어 포함 여부 검사
    if not re.search(r'[가-힣a-zA-Z0-9ぁ-んァ-ヶ一-龥]', pure_text): 
        return False

    return True

# AI 프롬프트용 텍스트 조립기 (List Join 방식)
def build_context_text(df, df_comments, category_name):
    # += 대신 리스트에 append 후 join 하는 것이 파이썬의 정석이자 훨씬 빠릅니다.
    text_chunks = [f"\n--- 📌 {category_name} ---\n"]

    for _, row in df.iterrows():
        text_chunks.append(f"제목: {row['title']}\n")

        if not df_comments.empty and row['post_uuid'] in df_comments['post_uuid'].values:
            post_comments = df_comments[df_comments['post_uuid'] == row['post_uuid']]
            for _, crow in post_comments.iterrows():
                text_chunks.append(f"  └ 베스트댓글({crow['upvote_count']}추천): {crow['content'][:100]}\n")

    return "".join(text_chunks)

# 업데이트 날짜 기준점 찾기 (공식 뉴스 기준)
def get_update_points(df):
    # 최상단에서 이미 깡통 방어를 했으므로, 여기서는 핵심 로직만 깔끔하게 남깁니다.
    df_official = df[df['plate_name'] == '공식 뉴스']
    updates = df_official[df_official['title'].str.contains('업데이트', na=False)]

    if len(updates) >= 2:
        sorted_updates = updates.sort_values('created_at', ascending=False)
        return sorted_updates.iloc[0]['created_at'], sorted_updates.iloc[1]['created_at']

    # 공식 뉴스 업데이트 글이 부족할 때의 백업 기본값
    now_ts = int(datetime.now().timestamp())
    prev_ts = now_ts - (86400 * 7) # 7일(초 단위) 전
    return now_ts, prev_ts

# ==========================================
# 2-3. 변수 할당 및 후속 처리
# ==========================================
curr_update_ts, prev_update_ts = get_update_points(df_all)

# ==========================================
# 3. 메인 UI 구성 & 사이드바
# ==========================================
st.title("Blablalink 인사이트 대시보드")

st.sidebar.header("🔍 분석 설정")
# 선택된 값을 변수에 제대로 담아줍니다
analysis_mode = st.sidebar.selectbox("비교 기준", ["이전 업데이트 대비 (UoU)", "전일 대비 (DoD)"])

st.sidebar.markdown("---")
st.sidebar.header("🤖 AI 분석 엔진")
gemini_api_key = st.sidebar.text_input("Gemini API Key를 입력하세요", type="password")

df_out_pure = df_all[(df_all['plate_name'] == '전초기지') & (df_all['is_official'] == False)].copy()
df_guide = df_all[(df_all['plate_name'] == '유저 공략')].copy()

tab_outpost, tab_guide, tab_art = st.tabs(["🗣️ 전초기지 (여론)", "📚 유저 공략 (트렌드)", "🎨 니케 아트 (미디어)"])

# --- TAB 1: 전초기지 (다이내믹 필터링 적용) ---
with tab_outpost:

    # 모드에 따른 동적 시간 필터링 계산
    now_dt = datetime.now(df_out_pure['created_at_dt'].dt.tz)

    # 업데이트 기준일 (UTC -> KST 변환 보장)
    curr_update_dt = pd.to_datetime(curr_update_ts, unit='s', utc=True).tz_convert('Asia/Seoul')
    prev_update_dt = pd.to_datetime(prev_update_ts, unit='s', utc=True).tz_convert('Asia/Seoul')

    if "UoU" in analysis_mode:
        # UoU 모드: 이번 패치 이후 vs 지난 패치 기간
        current_df = df_out_pure[df_out_pure['created_at_dt'] >= curr_update_dt]
        compare_df = df_out_pure[(df_out_pure['created_at_dt'] >= prev_update_dt) & 
                                 (df_out_pure['created_at_dt'] < curr_update_dt)]
        time_label = f"이번 업데이트 ({curr_update_dt.strftime('%m/%d')}~)"
        delta_label = "이전 업데이트 대비"
        chart_group = 'date' # UoU는 '일별'로 그룹핑
    else:
        # DoD 모드: 최근 24시간 vs 직전 24시간
        current_df = df_out_pure[df_out_pure['created_at_dt'] >= (now_dt - timedelta(hours=24))]
        compare_df = df_out_pure[(df_out_pure['created_at_dt'] >= (now_dt - timedelta(hours=48))) & 
                                 (df_out_pure['created_at_dt'] < (now_dt - timedelta(hours=24)))]
        time_label = "최근 24시간"
        delta_label = "전일 24h 대비"
        # 💡 DoD는 기간이 짧으므로 차트를 '시간별'로 보여줍니다
        current_df['hour'] = current_df['created_at_dt'].dt.strftime('%Y-%m-%d %H:00') 
        chart_group = 'hour'

    st.markdown(f"**기준:** {time_label} (분석 대상: {len(current_df):,}건)")

    # 2. KPI 계산 도우미 함수
    pos_words = ['갓겜', '혜자', '만족', '재밌', '좋아', '좋다', '대박', '최고', '기대', '기쁘다', '기뻤습니다', '기뻐요', '기쁩니다', '행복', '행복하다', '행복해']
    neg_words = ['망겜', '창렬', '불만', '싫어', '싫다', '노답', '삭제', '접음', '최악', '나쁘다', '나빴습니다', '나빠요', '나쁩니다', '슬픔', '슬프다', '슬퍼'
                ,'문제']
    risk_keywords = ['버그', '튕김', '팅김', '크래쉬', '불만', '어려움', '불합리', '점검', '오류', '문제', '이슈', '접속할 수 없']
    # [04-17 추가] 2차 필터링: 이 단어가 포함되어 있다면 단순 토론/푸념으로 간주하고 면제해 줌
    exclude_keywords = ['가챠', '뽑기', '캐릭터', '외모', '성능', '확률', '운']

    def get_sentiment(df):
        if df.empty: return 0.0, df.copy()
        df['s_score'] = df['title'].apply(lambda x: sum(1 for w in pos_words if w in str(x)) - sum(1 for w in neg_words if w in str(x)))
        pos = len(df[df['s_score'] > 0])
        neg = len(df[df['s_score'] < 0])
        tot = pos + neg
        return (pos / tot * 100) if tot > 0 else 0.0, df

    curr_sent_pct, current_df = get_sentiment(current_df)
    comp_sent_pct, _ = get_sentiment(compare_df)
    sent_delta = curr_sent_pct - comp_sent_pct

    current_df['is_risk'] = current_df['title'].str.contains('|'.join(risk_keywords), na=False)
    compare_df['is_risk'] = compare_df['title'].str.contains('|'.join(risk_keywords), na=False)

    curr_risk_cnt = current_df['is_risk'].sum()
    comp_risk_cnt = compare_df['is_risk'].sum()
    risk_delta = int(curr_risk_cnt - comp_risk_cnt)

    # 3. 레이아웃 배치
    top_col1, top_col2 = st.columns(2)

    with top_col1:
        st.subheader("📈 Sentiment Score")
        st.metric(label="유저 긍정 지수", 
                  value=f"{curr_sent_pct:.1f}%", 
                  delta=f"{sent_delta:+.1f}%p ({delta_label})")
        # 동적 차트 출력 (UoU=일별, DoD=시간별)
        if not current_df.empty:
            sent_trend = current_df.groupby(chart_group)['s_score'].mean()
            st.line_chart(sent_trend, height=200)

    with top_col2:
        # [04-17 추가] help 파라미터를 이용해 i 아이콘 툴팁 생성
        st.subheader("🚨 Risk Spike", help=f"감지 대상 키워드: {', '.join(risk_keywords)}")

        st.metric(label="리스크 키워드 감지", 
                  value=f"{curr_risk_cnt} 건", 
                  delta=f"{risk_delta} 건 ({delta_label})", 
                  delta_color="inverse")

        if not current_df.empty:
            risk_trend = current_df[current_df['is_risk']].groupby(chart_group).size()
            st.line_chart(risk_trend, height=200)

            # [04-17 추가] 리스크 감지 게시글 링크 제공 (원인 분석)
            if curr_risk_cnt > 0:
                st.markdown("#### 🔗 감지된 주요 리스크 게시글")

                # 최신순으로 정렬 최대 3개만 표시 (UI가 너무 길어지는 것 방지)
                risk_posts = current_df[current_df['is_risk']].sort_values(by='created_at_dt', ascending=False).head(3)

                for idx, row in risk_posts.iterrows():
                    post_url = f"https://www.blablalink.com/post/detail?post_uuid={row['post_uuid']}"

                    # 제목에서 어떤 리스크 키워드가 걸렸는지 역추적
                    caught_keywords = [w for w in risk_keywords if w in str(row['title'])]
                    keyword_tags = ", ".join(caught_keywords)

                    # 마크다운을 이용한 하이퍼링크 및 태그 출력
                    st.markdown(f"- [{row['title']}]({post_url})  🚨`{keyword_tags}`")

    st.divider()

    # 4. 하단 AI 전략 분석 센터
    st.header("통합 전략 분석 리포트")

    # [시각화 추가] 긍정 지수 변화 (Gauge Chart)
    col_sent1, col_sent2 = st.columns([1, 2])
    with col_sent1:
        fig_gauge = go.Figure(go.Indicator(
            mode = "gauge+number+delta",
            value = curr_sent_pct,
            domain = {'x': [0, 1], 'y': [0, 1]},
            title = {'text': "현재 긍정 지수 (%)"},
            delta = {'reference': comp_sent_pct, 'increasing': {'color': "green"}},
            gauge = {
                'axis': {'range': [0, 100]},
                'bar': {'color': "royalblue"},
                'steps': [
                    {'range': [0, 40], 'color': "lightpink"},
                    {'range': [40, 70], 'color': "lightyellow"},
                    {'range': [70, 100], 'color': "lightgreen"}]
            }
        ))
        fig_gauge.update_layout(height=300, margin=dict(l=20, r=20, t=50, b=20))
        st.plotly_chart(fig_gauge, width='stretch')

    with col_sent2:
        st.markdown("### 실시간 이슈 통합 리포트")
        if st.button("통합 민심 전략 리포트 생성", type="primary"):
            if not gemini_api_key:
                st.error("API Key를 입력하세요.")
            else:
                with st.spinner('방대한 데이터와 핵심 댓글을 교차 분석 중입니다...'):
                    try:
                        # [데이터 정제 1] 타겟 게시글 선정 (전초기지, 리스크, 공략)
                        outpost_top = df_out_pure.sort_values(['comment_count', 'created_at_dt'], ascending=False).head(20)
                        risk_top = current_df[current_df['is_risk']].head(10)
                        guide_top = df_guide.nlargest(5, 'browse_count') # 공략 탭 추가

                        # 모든 타겟 UUID 수집 (N+1 쿼리 방지를 위한 준비)
                        target_posts = pd.concat([outpost_top, risk_top, guide_top]).drop_duplicates('post_uuid')
                        target_uuids = target_posts['post_uuid'].dropna().unique().tolist()

                        df_comments = pd.DataFrame()
                        if target_uuids:
                            # in_ 연산자를 사용하여 1번의 쿼리로 관련 댓글 전체 수집
                            res_comments = supabase.table("comments").select("post_uuid, content, upvote_count").in_("post_uuid", target_uuids).execute()
                            if res_comments.data:
                                df_comments = pd.DataFrame(res_comments.data)

                        if not df_comments.empty:
                            df_comments = df_comments[df_comments['content'].apply(is_valid_comment)]
                            # 각 게시글별 추천수 상위 3개 댓글만 추출 (메모리상에서 처리)
                            df_comments = df_comments.sort_values('upvote_count', ascending=False).groupby('post_uuid').head(3)

                        final_context = ""
                        final_context += build_context_text(outpost_top, df_comments, "일반 여론 (전초기지)")
                        final_context += build_context_text(risk_top, df_comments, "리스크 감지 게시글")
                        final_context += build_context_text(guide_top, df_comments, "유저 공략 트렌드")

                        # 니케 백과사전 읽어오기
                        nikke_base = ""
                        try:
                            with open("nikke_base.md", "r", encoding="utf-8") as f:
                                nikke_base = f.read()
                        except FileNotFoundError:
                            nikke_base = "사전 파일이 없습니다."

                        prompt = f"""
                        너는 '승리의 여신: 니케'의 시니어 전략 분석가야. 
                        다음은 네가 분석할 때 반드시 참고해야 할 [인게임 배경지식]이야.
                        {nikke_base}

                        다음 데이터를 종합하여 경영진용 [전략 분석 리포트]를 작성해.
                        [분석 데이터]
                        {final_context}

                        [보고서 필수 포함 항목]
                        - **핵심 여론 요약**: 현재 유저들이 가장 열광하거나 분노하는 지점이 무엇인지 베스트 댓글을 근거로 3줄 이내 요약.
                        - **리스크 심층 분석**: 리스크 게시글들이 실제 시스템적 결함인지, 단순 감정적 불만인지 구분하여 분석.
                        - **유저 공략 트렌드**: 유저들이 현재 어떤 콘텐츠(공략)에 집중하고 있는지 파악.
                        - **미래 전략**: 핵심 여론, 리스크 분석, 공략 트렌드 분석을 기반으로 향후 업데이트 방향성 및 운영 전략을 제시.
                        """

                        client = genai.Client(api_key=gemini_api_key)
                        response = client.models.generate_content(model='gemini-2.5-flash', contents=prompt)
                        st.info("AI 리포트 결과")
                        st.markdown(response.text)
                    except Exception as e:
                        st.error(f"리포트 생성 실패: {e}")

# --- TAB 2: 유저 공략 ---
with tab_guide:

    # 1. 🕒 시간 필터링 데이터 준비
    now_dt = datetime.now(df_guide['created_at_dt'].dt.tz)
    curr_update_dt = pd.to_datetime(curr_update_ts, unit='s', utc=True).tz_convert('Asia/Seoul')
    prev_update_dt = pd.to_datetime(prev_update_ts, unit='s', utc=True).tz_convert('Asia/Seoul')

    # UoU 데이터 (이번 업데이트 vs 이전 업데이트)
    uou_curr = df_guide[df_guide['created_at_dt'] >= curr_update_dt]
    uou_prev = df_guide[(df_guide['created_at_dt'] >= prev_update_dt) & (df_guide['created_at_dt'] < curr_update_dt)]

    # DoD 데이터 (최근 24시간 vs 직전 24시간)
    dod_curr = df_guide[df_guide['created_at_dt'] >= (now_dt - timedelta(hours=24))].copy()
    dod_prev = df_guide[(df_guide['created_at_dt'] >= (now_dt - timedelta(hours=48))) & (df_guide['created_at_dt'] < (now_dt - timedelta(hours=24)))]

    # 2. 지표 계산 함수
    def get_eng_rate(df):
        if df.empty or df['browse_count'].sum() == 0: return 0.0
        # 참여도 = (추천수 + 댓글수) / 조회수 * 100
        return (df['upvote_count'].sum() + df['comment_count'].sum()) / df['browse_count'].sum() * 100

    # 3. 상단 레이아웃 (1. 참여도 / 2. 트래픽)
    col1, col2 = st.columns(2)

    with col1:
        st.subheader("💡 Engagement Rate", help="참여도 = (좋아요 수 + 댓글 수) / 조회수")
        eng_curr = get_eng_rate(uou_curr)
        eng_prev = get_eng_rate(uou_prev)
        st.metric(label=f"이번 업데이트 참여도 ({curr_update_dt.strftime('%m/%d')}~)", 
                  value=f"{eng_curr:.2f}%", 
                  delta=f"{eng_curr - eng_prev:+.2f}%p (UoU)")

        # 참여도 일별 추이 (라인 그래프)
        if not uou_curr.empty:
            eng_trend = uou_curr.groupby('date').apply(get_eng_rate, include_groups=False)
            st.line_chart(eng_trend, height=200)

    with col2:
        st.subheader("📝 Traffic Shift")
        traf_curr = len(dod_curr)
        traf_prev = len(dod_prev)
        st.metric(label="최근 24시간 신규 게시글", 
                  value=f"{traf_curr} 건", 
                  delta=f"{traf_curr - traf_prev:+} 건 (DoD)")

        # 트래픽 시간별 추이 (라인 그래프)
        if not dod_curr.empty:
            dod_curr['hour'] = dod_curr['created_at_dt'].dt.strftime('%m-%d %H:00')
            traf_trend = dod_curr.groupby('hour').size()
            st.line_chart(traf_trend, height=200)

    st.divider()

    # 4. 중간 레이아웃: Share of Voice (키워드 추출)
    st.subheader("📊 Share of Voice (Top 5 키워드)")
    st.caption("최근 업데이트 이후 유저 공략 탭에서 가장 많이 언급된 단어입니다.")

    if not uou_curr.empty:
        # 명사 위주의 정규식 카운팅
        stop_words = ['공략', '니케', '뉴비', '질문', '이거', '어떻게', '진짜', '너무']
        all_text = " ".join(uou_curr['title'].astype(str).tolist())
        words = re.findall(r'[가-힣A-Za-z]{2,}', all_text) # 2글자 이상 한글/영문 추출
        filtered_words = [w for w in words if w not in stop_words]

        top_5_words = Counter(filtered_words).most_common(5)
        df_sov = pd.DataFrame(top_5_words, columns=['키워드', '빈도수'])

        # Plotly를 활용한 내림차순 & 디자인
        fig_sov = px.bar(df_sov, x='키워드', y='빈도수', text='빈도수', 
                         color='빈도수', color_continuous_scale='Blues')
        fig_sov.update_layout(xaxis={'categoryorder':'total descending'}, # 내림차순 강제 정렬
                              showlegend=False, height=250, margin=dict(l=0, r=0, t=30, b=0))
        st.plotly_chart(fig_sov, width='stretch')

    st.divider()

    # 5. 하단 레이아웃: TOP 3 인기 공략 (클릭 가능한 링크 포함)
    st.subheader(f"👑 업데이트 이후 TOP 3 인기 공략")
    if not uou_curr.empty:
        top3_df = uou_curr.nlargest(3, 'browse_count')
        for idx, row in top3_df.iterrows():
            # 네이버 게임 라운지 표준 URL 형식 (게시판 고유 ID 적용)
            post_url = f"https://www.blablalink.com/post/detail?post_uuid={row['post_uuid']}"

            # Streamlit Markdown을 이용해 클릭 가능한 하이퍼링크 생성
            st.markdown(f"""
            #### 🔗 [{row['title']}]({post_url})
            - **조회수:** {row['browse_count']:,} | **추천수:** {row['upvote_count']:,} | **댓글수:** {row['comment_count']:,} | **작성일:** {row['created_at_dt'].strftime('%Y-%m-%d %H:%M')}
            """)
    else:
        st.info("이번 업데이트 이후 작성된 공략글이 없습니다.")

# --- TAB 3: 니케 아트 (미디어 및 반응 분석) ---
with tab_art:
    # 1. Supabase 데이터 로드 (로컬 CSV 탈출)
    try:
        # DB에서 전체 데이터를 가져옵니다. 
        # (추후 데이터가 수만 건이 넘어가면 .select() 안에 필요한 컬럼만 명시하거나 필터링을 추가하여 최적화하세요.)
        response = supabase.table("nikke_arts").select("*").execute()

        if response.data:
            df_art = pd.DataFrame(response.data)

            # [데이터 정제] created_at이 numeric(float)으로 들어오므로 변환
            df_art['created_at'] = pd.to_numeric(df_art['created_at'], errors='coerce')
            df_art = df_art.dropna(subset=['created_at'])

            # 유효한 타임스탬프 범위 필터링
            df_art = df_art[(df_art['created_at'] > 1000000000) & (df_art['created_at'] < 2000000000)]

            # 시간대 변환 (UTC -> Asia/Seoul)
            df_art['created_at_dt'] = pd.to_datetime(df_art['created_at'], unit='s', utc=True).dt.tz_convert('Asia/Seoul')
            df_art['date'] = df_art['created_at_dt'].dt.date

            now_dt = datetime.now(df_art['created_at_dt'].dt.tz)

            # 2. DoD / UoU 동적 필터링 로직 (기존 유지)
            if "UoU" in analysis_mode:
                current_art = df_art[df_art['created_at_dt'] >= curr_update_dt].copy()
                compare_art = df_art[(df_art['created_at_dt'] >= prev_update_dt) & (df_art['created_at_dt'] < curr_update_dt)]
                days_curr = max((now_dt - curr_update_dt).days, 1)
                days_prev = max((curr_update_dt - prev_update_dt).days, 1)
                art_traf_curr = len(current_art) / days_curr
                art_traf_prev = len(compare_art) / days_prev
                art_label, art_delta_label = f"이번 업데이트", "이전 패치 일평균 대비"
                traf_label, traf_unit = "일평균 신규 게시글", "건/일"
                art_group = 'date'
            else:
                current_art = df_art[df_art['created_at_dt'] >= (now_dt - timedelta(hours=24))].copy()
                compare_art = df_art[(df_art['created_at_dt'] >= (now_dt - timedelta(hours=48))) & (df_art['created_at_dt'] < (now_dt - timedelta(hours=24)))]
                art_traf_curr = len(current_art)
                art_traf_prev = len(compare_art)
                art_label, art_delta_label = "최근 24시간", "전일 24h 대비"
                traf_label, traf_unit = "최근 24시간 신규 게시글", "건"
                current_art['hour'] = current_art['created_at_dt'].dt.strftime('%m-%d %H:00')
                art_group = 'hour'

            def get_art_eng_rate(df):
                if df.empty or df['browse_count'].sum() == 0: return 0.0
                return (df['upvote_count'].sum() + df['comment_count'].sum()) / df['browse_count'].sum() * 100

            # 3. 상단 레이아웃 (Metric)
            col1, col2 = st.columns(2)
            with col1:
                st.subheader("💡 Art Engagement Rate")
                art_eng_curr = get_art_eng_rate(current_art)
                art_eng_prev = get_art_eng_rate(compare_art)
                st.metric(label=f"참여도 ({art_label})", 
                          value=f"{art_eng_curr:.2f}%", 
                          delta=f"{art_eng_curr - art_eng_prev:+.2f}%p ({art_delta_label})")
                if not current_art.empty:
                    st.line_chart(current_art.groupby(art_group).apply(get_art_eng_rate, include_groups=False), height=200)

            with col2:
                st.subheader("🖼️ Art Traffic Shift")
                st.metric(label=traf_label, 
                          value=f"{art_traf_curr:.1f} {traf_unit}", 
                          delta=f"{art_traf_curr - art_traf_prev:+.1f} {traf_unit} ({art_delta_label})")
                if not current_art.empty:
                    st.line_chart(current_art.groupby(art_group).size(), height=200)

            st.divider()

            # 4. 중간 레이아웃 (태그 및 바이럴)
            col3, col4 = st.columns(2)
            with col3:
                st.subheader("🏷️ 인기 태그 TOP 5")
                if not current_art.empty:
                    all_tags = current_art['tags'].replace('태그 없음', None).dropna().str.split(',').explode().str.strip()
                    if not all_tags.empty:
                        top_tags = all_tags.value_counts().head(5).reset_index()
                        top_tags.columns = ['태그', '빈도수']
                        fig_tags = px.bar(top_tags, x='태그', y='빈도수', text='빈도수', color='빈도수', color_continuous_scale='Purples')
                        fig_tags.update_layout(xaxis={'categoryorder':'total descending'}, showlegend=False, height=350, margin=dict(l=0, r=0, t=30, b=0))
                        st.plotly_chart(fig_tags, width='stretch')

            with col4:
                st.subheader("🔄 바이럴 트렌드 (공유 횟수)")
                if not current_art.empty:
                    st.line_chart(current_art.groupby(art_group)['forward_count'].sum(), height=350)

            st.divider()

            # 5. 하단 레이아웃: 인기 창작물 (이미지 렌더링 추가)
            st.subheader("🎨 이번 업데이트 인기 창작물 TOP 3")
            if not current_art.empty:
                unique_posts = current_art.drop_duplicates(subset=['post_uuid']).copy()
                unique_posts['popularity_score'] = unique_posts['browse_count'] + (unique_posts['upvote_count'] * 10)
                top3_art = unique_posts.nlargest(3, 'popularity_score')

                # 가로로 3개의 컬럼을 만들어 이미지를 배치
                art_cols = st.columns(3)
                for i, (idx, row) in enumerate(top3_art.iterrows()):
                    with art_cols[i]:
                        # 💡 다중 이미지 처리: 파이프(|)로 쪼개서 첫 번째 이미지만 썸네일로 활용
                        img_urls = str(row['image_url']).split('|')
                        display_img = img_urls[0] if img_urls[0] != "NO_IMAGE" else None

                        if display_img:
                            st.image(display_img, width='stretch')
                        else:
                            st.info("이미지를 불러올 수 없는 게시글입니다.")

                        post_url = f"https://www.blablalink.com/post/detail?post_uuid={row['post_uuid']}"
                        st.markdown(f"**[{row['title']}]({post_url})**")
                        st.caption(f"👁️ {row['browse_count']:,} | 👍 {row['upvote_count']:,} | 🔄 {row['forward_count']:,}")
        else:
            st.info("Supabase DB에 적재된 아트 데이터가 없습니다.")

    except Exception as e:
        st.error(f"데이터베이스 연결 오류: {e}")
