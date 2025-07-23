import streamlit as st
import pandas as pd
from datetime import datetime
import os
from dotenv import load_dotenv
from azure.storage.blob import BlobServiceClient
from azure.search.documents import SearchClient
from azure.core.credentials import AzureKeyCredential
import openai
import json
import plotly.express as px
import plotly.graph_objects as go
from io import StringIO

# 환경 변수 로드
load_dotenv()

# Azure 설정
STORAGE_CONNECTION_STRING = os.getenv("STORAGE_CONNECTION_STRING")
SEARCH_ENDPOINT = os.getenv("SEARCH_ENDPOINT")
SEARCH_KEY = os.getenv("SEARCH_KEY")
SEARCH_INDEX_NAME = "ito-events-index"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_ENDPOINT = os.getenv("OPENAI_ENDPOINT")
OPENAI_DEPLOYMENT = os.getenv("OPENAI_DEPLOYMENT")

# Azure OpenAI 설정
OPENAI_CLIENT = None
OPENAI_VERSION = None

if OPENAI_API_KEY and OPENAI_ENDPOINT:
    try:
        # 버전 확인
        import openai
        openai_version = openai.__version__
        
        if openai_version.startswith('0.'):
            # 구버전 (0.28.x)
            openai.api_type = "azure"
            openai.api_base = OPENAI_ENDPOINT
            openai.api_version = "2024-02-01"
            openai.api_key = OPENAI_API_KEY
            OPENAI_VERSION = "0.28"
        else:
            # 신버전 (1.0+)
            try:
                from openai import AzureOpenAI
                client = AzureOpenAI(
                    api_key=OPENAI_API_KEY,
                    api_version="2024-02-01",
                    azure_endpoint=OPENAI_ENDPOINT
                )
                OPENAI_CLIENT = client
                OPENAI_VERSION = "1.0+"
            except Exception as e:
                # 신버전 초기화 실패 시 구버전 방식 시도
                st.warning(f"OpenAI 클라이언트 초기화 실패: {str(e)}")
                OPENAI_VERSION = None
    except ImportError:
        st.error("OpenAI 라이브러리가 설치되지 않았습니다. 'pip install openai==0.28.1' 명령으로 설치하세요.")
        OPENAI_VERSION = None

# 페이지 설정
st.set_page_config(
    page_title="ITO 이벤트 분석 agent",
    page_icon="📊",
    layout="wide"
)

# 스타일 설정
st.markdown("""
<style>
    .stButton > button {
        background-color: #0066cc;
        color: white;
        border-radius: 5px;
        padding: 0.5rem 1rem;
        border: none;
        transition: background-color 0.3s;
    }
    .stButton > button:hover {
        background-color: #0052a3;
    }
    .metric-card {
        background-color: #f0f0f0;
        padding: 20px;
        border-radius: 10px;
        text-align: center;
        margin: 10px 0;
    }
</style>
""", unsafe_allow_html=True)

# 헬퍼 함수들 정의
def calculate_average_duration(duration_series):
    """Duration 문자열을 파싱하여 평균 계산"""
    try:
        total_seconds = 0
        count = 0
        
        for duration in duration_series:
            if pd.notna(duration) and duration != '':
                # Duration 형식 파싱 (예: "1h 30m", "45m", "2d 3h")
                seconds = parse_duration_to_seconds(duration)
                if seconds > 0:
                    total_seconds += seconds
                    count += 1
        
        if count > 0:
            avg_seconds = total_seconds / count
            return format_seconds_to_duration(avg_seconds)
        return "N/A"
    except:
        return "N/A"

def parse_duration_to_seconds(duration_str):
    """Duration 문자열을 초 단위로 변환"""
    try:
        total_seconds = 0
        parts = duration_str.strip().split()
        
        for part in parts:
            if 'd' in part:
                days = int(part.replace('d', ''))
                total_seconds += days * 86400
            elif 'h' in part:
                hours = int(part.replace('h', ''))
                total_seconds += hours * 3600
            elif 'm' in part:
                minutes = int(part.replace('m', ''))
                total_seconds += minutes * 60
            elif 's' in part:
                seconds = int(part.replace('s', ''))
                total_seconds += seconds
                
        return total_seconds
    except:
        return 0

def format_seconds_to_duration(seconds):
    """초를 읽기 쉬운 형식으로 변환"""
    if seconds < 60:
        return f"{int(seconds)}초"
    elif seconds < 3600:
        return f"{int(seconds/60)}분"
    elif seconds < 86400:
        hours = int(seconds/3600)
        minutes = int((seconds % 3600) / 60)
        return f"{hours}시간 {minutes}분"
    else:
        days = int(seconds/86400)
        hours = int((seconds % 86400) / 3600)
        return f"{days}일 {hours}시간"

# 간략화된 AI 분석 함수
def perform_simple_ai_analysis(df):
    """간단한 AI 종합 분석"""
    try:
        # OpenAI 설정 확인
        if not OPENAI_VERSION:
            return "AI 분석을 사용할 수 없습니다. OpenAI 설정을 확인하세요."
        
        # 데이터 요약
        total_events = len(df)
        
        # Severity 컬럼 찾기
        severity_col = None
        for col in ['Severity', 'severity', 'SEVERITY', '심각도', 'Level', 'level']:
            if col in df.columns:
                severity_col = col
                break
        
        if severity_col:
            df_severity = df.copy()
            df_severity[severity_col] = df_severity[severity_col].astype(str).str.lower()
            critical_events = len(df_severity[df_severity[severity_col].isin(['disaster', 'high', 'fatal', 'critical', 'error'])])
            warning_events = len(df_severity[df_severity[severity_col].isin(['average', 'warning', 'major', 'medium'])])
        else:
            critical_events = 0
            warning_events = 0
        
        unique_hosts = df['Host'].nunique() if 'Host' in df.columns else 0
        
        # 상위 문제
        top_issues = df['Description'].value_counts().head(10).to_dict() if 'Description' in df.columns else {}
        
        # 시간 분석
        time_analysis = {}
        if 'Time' in df.columns:
            df_time = df.copy()
            df_time['Time'] = pd.to_datetime(df_time['Time'])
            df_time['Hour'] = df_time['Time'].dt.hour
            hourly_counts = df_time.groupby('Hour').size()
            peak_hours = hourly_counts.nlargest(3).to_dict()
            
            # 최근 24시간 vs 이전 24시간 비교
            latest_time = df_time['Time'].max()
            recent_24h = len(df_time[df_time['Time'] >= latest_time - pd.Timedelta(hours=24)])
            previous_24h = len(df_time[(df_time['Time'] >= latest_time - pd.Timedelta(hours=48)) & 
                                      (df_time['Time'] < latest_time - pd.Timedelta(hours=24))])
            
            time_analysis = {
                "peak_hours": peak_hours,
                "recent_24h": recent_24h,
                "previous_24h": previous_24h,
                "trend": "증가" if recent_24h > previous_24h else "감소",
                "change_rate": ((recent_24h - previous_24h) / previous_24h * 100) if previous_24h > 0 else 0
            }
        
        # 호스트별 분석
        host_analysis = {}
        if 'Host' in df.columns:
            host_counts = df['Host'].value_counts()
            top_hosts = host_counts.head(5).to_dict()
            avg_events_per_host = total_events / unique_hosts if unique_hosts > 0 else 0
            
            # 문제가 많은 호스트
            if 'Status' in df.columns:
                problem_hosts = df[df['Status'] == 'PROBLEM']['Host'].value_counts().head(5).to_dict()
            else:
                problem_hosts = {}
            
            host_analysis = {
                "top_hosts": top_hosts,
                "avg_events_per_host": avg_events_per_host,
                "problem_hosts": problem_hosts
            }
        
        # 샘플 데이터 준비
        sample_df = df.head(100).copy()
        if 'Time' in sample_df.columns:
            sample_df['Time'] = sample_df['Time'].astype(str)
        sample_data = sample_df.to_dict('records')
        
        prompt = f"""
        다음 Zabbix/ITO 이벤트 데이터를 상세히 분석해주세요:
        
        === 데이터 요약 ===
        총 이벤트: {total_events}
        심각 이벤트 (fatal/critical/high): {critical_events} ({(critical_events/total_events*100):.1f}%)
        경고 이벤트 (major/warning): {warning_events} ({(warning_events/total_events*100):.1f}%)
        영향 호스트: {unique_hosts}
        
        === 시간 분석 ===
        {json.dumps(time_analysis, ensure_ascii=False, indent=2)}
        
        === 호스트 분석 ===
        {json.dumps(host_analysis, ensure_ascii=False, indent=2)}
        
        === 주요 문제 Top 10 ===
        {json.dumps(top_issues, ensure_ascii=False, indent=2)}
        
        === 샘플 이벤트 데이터 ===
        {json.dumps(sample_data[:50], ensure_ascii=False, indent=2)[:3000]}
        
        다음 형식으로 상세하고 구조화된 분석을 제공해주세요:
        
        ## 1. 🔍 현재 시스템 상태 진단
        ### 전반적 상태: [양호/주의/경고/위험]
        - 종합 평가와 근거
        - 주요 지표별 상태
        - 즉각적인 주의가 필요한 사항
        
        ## 2. 🚨 주요 문제점 분석 (우선순위순)
        ### 문제 1: [문제명]
        - 영향도: [높음/중간/낮음]
        - 발생 빈도: X건 (Y%)
        - 영향받는 시스템/호스트: 
        - 예상 원인:
        - 권장 조치:
        
        ### 문제 2: [문제명]
        - (동일 형식)
        
        ### 문제 3: [문제명]
        - (동일 형식)
        
        ## 3. 📊 패턴 및 트렌드 분석
        ### 시간적 패턴
        - 피크 시간대와 원인 분석
        - 24시간 트렌드 (증가/감소 및 변화율)
        - 주기적 패턴 유무
        
        ### 호스트별 패턴
        - 문제가 집중된 호스트
        - 호스트 간 상관관계
        - 특이 패턴 발견사항
        
        ## 4. 🎯 즉시 조치사항 (Action Items)
        ### 긴급 (24시간 내)
        1. [구체적 조치사항]
        2. [구체적 조치사항]
        
        ### 단기 (1주일 내)
        1. [구체적 조치사항]
        2. [구체적 조치사항]
        
        ### 중장기 개선사항
        1. [구체적 조치사항]
        2. [구체적 조치사항]
        
        ## 5. 💡 추가 권장사항
        - 모니터링 강화 포인트
        - 임계값 조정 제안
        - 프로세스 개선 제안
        
        각 섹션을 구체적이고 실행 가능한 내용으로 작성하고, 데이터에 기반한 정량적 근거를 포함해주세요.
        """
        
        # OpenAI API 호출 (버전별 분기)
        if OPENAI_VERSION == "1.0+" and OPENAI_CLIENT:
            # 새로운 API (1.0+)
            response = OPENAI_CLIENT.chat.completions.create(
                model=OPENAI_DEPLOYMENT,
                messages=[
                    {"role": "system", "content": "당신은 IT 인프라 모니터링 및 장애 분석 전문가입니다. 데이터를 기반으로 구체적이고 실행 가능한 인사이트를 제공합니다."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=2000,
                temperature=0.7
            )
            return response.choices[0].message.content
        elif OPENAI_VERSION == "0.28":
            # 구버전 API (0.28)
            response = openai.ChatCompletion.create(
                engine=OPENAI_DEPLOYMENT,
                messages=[
                    {"role": "system", "content": "당신은 IT 인프라 모니터링 및 장애 분석 전문가입니다. 데이터를 기반으로 구체적이고 실행 가능한 인사이트를 제공합니다."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=2000,
                temperature=0.7
            )
            return response.choices[0].message.content
        else:
            return "AI 분석을 사용할 수 없습니다. OpenAI 설정을 확인하세요."
        
    except Exception as e:
        return f"분석 중 오류가 발생했습니다: {str(e)}"

def create_visualizations(df):
    """데이터 시각화"""
    st.subheader("📈 시각화 대시보드")
    
    # DataFrame 복사본 생성
    df = df.copy()
    
    col1, col2 = st.columns(2)
    
    with col1:
        # Severity별 분포
        if 'Severity' in df.columns:
            severity_counts = df['Severity'].value_counts()
            
            # 색상 매핑 (Zabbix 표준)
            colors = {
                'Disaster': '#E45959',
                'High': '#E97659',
                'Average': '#FFA059',
                'Warning': '#FFC859',
                'Information': '#7499FF',
                'Not classified': '#97AAB3'
            }
            
            fig = px.pie(
                values=severity_counts.values, 
                names=severity_counts.index,
                title='심각도별 이벤트 분포',
                color=severity_counts.index,
                color_discrete_map=colors
            )
            st.plotly_chart(fig, use_container_width=True)
        
        # Host별 이벤트 수
        if 'Host' in df.columns:
            top_hosts = df['Host'].value_counts().head(10)
            fig = px.bar(
                x=top_hosts.values,
                y=top_hosts.index,
                orientation='h',
                title='호스트별 이벤트 발생 수 (Top 10)',
                labels={'x': '이벤트 수', 'y': '호스트'}
            )
            st.plotly_chart(fig, use_container_width=True)
    
    with col2:
        # 시간별 이벤트 발생 추이
        if 'Time' in df.columns:
            df['Time'] = pd.to_datetime(df['Time'])
            
            # 시간대별 집계
            df['Hour'] = df['Time'].dt.hour
            hourly_counts = df.groupby(['Hour', 'Severity']).size().reset_index(name='count')
            
            fig = px.line(
                hourly_counts,
                x='Hour',
                y='count',
                color='Severity',
                title='시간대별 이벤트 발생 추이',
                labels={'Hour': '시간', 'count': '이벤트 수'}
            )
            st.plotly_chart(fig, use_container_width=True)
        
        # Status별 분포
        if 'Status' in df.columns:
            status_counts = df['Status'].value_counts()
            fig = px.pie(
                values=status_counts.values,
                names=status_counts.index,
                title='이벤트 상태 분포',
                color_discrete_map={'OK': '#59DB8F', 'PROBLEM': '#E45959'}
            )
            st.plotly_chart(fig, use_container_width=True)

def perform_analysis(analysis_type, time_range):
    """선택된 분석 수행"""
    with st.spinner(f"{analysis_type} 수행 중..."):
        # 여기에 실제 분석 로직 구현
        st.success(f"✅ {analysis_type} 완료!")
        
        # 샘플 결과
        st.markdown(f"""
        ### {analysis_type} 결과
        
        **분석 기간**: {time_range}
        
        **주요 발견사항**:
        - 패턴 1: 특정 시간대에 이벤트 집중
        - 패턴 2: 특정 서비스의 반복적 오류
        - 패턴 3: 리소스 사용량 증가 추세
        
        **권장사항**:
        1. 피크 시간대 리소스 증설 고려
        2. 오류 발생 서비스 점검 필요
        3. 예방적 모니터링 강화
        """)

def generate_report(report_type):
    """종합 리포트 생성"""
    # 데이터 확인
    if 'event_data' not in st.session_state and 'event_files' not in st.session_state:
        st.error("리포트를 생성할 데이터가 없습니다. 먼저 데이터를 업로드하세요.")
        return
    
    with st.spinner("종합 리포트 생성 중..."):
        # 데이터 가져오기
        if 'event_data' in st.session_state:
            df = st.session_state['event_data'].copy()
        else:
            dfs = [data['data'] for data in st.session_state['event_files'].values()]
            df = pd.concat(dfs, ignore_index=True)
        
        # 시간 처리
        if 'Time' in df.columns:
            df['Time'] = pd.to_datetime(df['Time'])
        
        # 통계 계산
        total_events = len(df)
        
        # Severity 컬럼 찾기
        severity_col = None
        for col in ['Severity', 'severity', 'SEVERITY', '심각도', 'Level', 'level']:
            if col in df.columns:
                severity_col = col
                break
        
        if severity_col:
            critical_events = len(df[df[severity_col].str.lower().isin(['disaster', 'high', 'fatal', 'critical', 'error'])]) 
            warning_events = len(df[df[severity_col].str.lower().isin(['average', 'warning', 'major', 'medium'])])
            info_events = len(df[df[severity_col].str.lower().isin(['information', 'info', 'low', 'not classified'])])
        else:
            critical_events = 0
            warning_events = 0
            info_events = 0
        
        problem_events = len(df[df['Status'] == 'PROBLEM']) if 'Status' in df.columns else 0
        ok_events = len(df[df['Status'] == 'OK']) if 'Status' in df.columns else 0
        
        unique_hosts = df['Host'].nunique() if 'Host' in df.columns else 0
        top_hosts = df['Host'].value_counts().head(10) if 'Host' in df.columns else pd.Series()
        
        # 시간 정보
        if 'Time' in df.columns:
            time_range = f"{df['Time'].min().strftime('%Y-%m-%d %H:%M')} ~ {df['Time'].max().strftime('%Y-%m-%d %H:%M')}"
            total_days = (df['Time'].max() - df['Time'].min()).days + 1
            peak_hour = df.groupby(df['Time'].dt.hour).size().idxmax()
            
            # 일별 통계
            daily_stats = df.groupby(df['Time'].dt.date).size()
            daily_avg = daily_stats.mean()
            daily_max = daily_stats.max()
            daily_min = daily_stats.min()
            
            # 시간대별 분포
            hourly_dist = df.groupby(df['Time'].dt.hour).size()
        else:
            time_range = "N/A"
            total_days = 0
            peak_hour = "N/A"
            daily_avg = 0
            daily_max = 0
            daily_min = 0
            hourly_dist = pd.Series()
        
        # Duration 분석
        if 'Duration' in df.columns:
            df['Duration_seconds'] = df['Duration'].apply(parse_duration_to_seconds)
            avg_duration = df[df['Duration_seconds'] > 0]['Duration_seconds'].mean()
            max_duration = df[df['Duration_seconds'] > 0]['Duration_seconds'].max()
            avg_duration_str = format_seconds_to_duration(avg_duration) if not pd.isna(avg_duration) else "N/A"
            max_duration_str = format_seconds_to_duration(max_duration) if not pd.isna(max_duration) else "N/A"
        else:
            avg_duration_str = "N/A"
            max_duration_str = "N/A"
        
        # 상위 문제
        top_issues = df['Description'].value_counts().head(15) if 'Description' in df.columns else pd.Series()
        
        # 리포트 내용 생성
        report_content = f"""# ITO 이벤트 종합 리포트
        
생성일시: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
분석 기간: {time_range}
총 분석 일수: {total_days}일

================================================================================

## 📊 전체 요약
- 총 이벤트 수: {total_events:,}
- 일평균 이벤트: {daily_avg:.1f}건
- 일 최대 이벤트: {daily_max:,}건
- 일 최소 이벤트: {daily_min:,}건

## 🚨 심각도 분석
- 심각 이벤트 (Fatal/Critical/High): {critical_events:,}건 ({(critical_events/total_events*100):.1f}%)
- 경고 이벤트 (Major/Average/Warning): {warning_events:,}건 ({(warning_events/total_events*100):.1f}%)
- 정보 이벤트 (Information/Low): {info_events:,}건 ({(info_events/total_events*100):.1f}%)
- 해결된 이벤트 (OK): {ok_events:,}건
- 문제 이벤트 (PROBLEM): {problem_events:,}건

## 🖥️ 호스트 분석
- 영향받은 호스트 수: {unique_hosts}개
- 호스트당 평균 이벤트: {(total_events/unique_hosts):.1f}건

### 가장 많은 이벤트 발생 호스트 Top 10:
"""
        
        for i, (host, count) in enumerate(top_hosts.items(), 1):
            percentage = (count / total_events * 100)
            report_content += f"  {i:2d}. {host}: {count:,}건 ({percentage:.1f}%)\n"
        
        report_content += f"""
## ⏰ 시간 분석
- 피크 시간대: {peak_hour}시
- 평균 이벤트 지속 시간: {avg_duration_str}
- 최대 이벤트 지속 시간: {max_duration_str}

### 시간대별 이벤트 분포:
"""
        
        # 시간대별 분포 추가
        if not hourly_dist.empty:
            peak_hours = hourly_dist.nlargest(5)
            for hour, count in peak_hours.items():
                report_content += f"  - {hour:02d}시: {count:,}건\n"
        
        report_content += """
## 🚨 주요 문제 (Top 15)
"""
        for i, (issue, count) in enumerate(top_issues.items(), 1):
            percentage = (count / total_events * 100)
            report_content += f"{i:2d}. {issue[:100]}{'...' if len(issue) > 100 else ''}\n"
            report_content += f"    - 발생 횟수: {count:,}건 ({percentage:.1f}%)\n"
        
        report_content += """
================================================================================
"""
        
        st.success("✅ 종합 리포트 생성 완료!")
        
        # 리포트 미리보기
        with st.expander("📄 리포트 미리보기", expanded=True):
            st.text(report_content)
        
        # 다운로드 버튼
        col1, col2, col3 = st.columns([1, 1, 2])
        with col1:
            st.download_button(
                label="📥 텍스트 리포트 다운로드",
                data=report_content,
                file_name=f"ITO_종합리포트_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
                mime="text/plain",
                type="primary"
            )
        
        with col2:
            # CSV 형식으로도 다운로드 가능
            summary_data = {
                '항목': ['총 이벤트', '일평균 이벤트', '심각 이벤트', '경고 이벤트', '정보 이벤트', '영향 호스트'],
                '값': [f"{total_events:,}", f"{daily_avg:.1f}", f"{critical_events:,}", f"{warning_events:,}", f"{info_events:,}", f"{unique_hosts}"],
                '비율': ['-', '-', f"{(critical_events/total_events*100):.1f}%", f"{(warning_events/total_events*100):.1f}%", f"{(info_events/total_events*100):.1f}%", '-']
            }
            summary_df = pd.DataFrame(summary_data)
            
            st.download_button(
                label="📥 요약 데이터 다운로드 (CSV)",
                data=summary_df.to_csv(index=False, encoding='utf-8-sig'),
                file_name=f"ITO_요약데이터_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                mime="text/csv"
            )

# 연결 상태 확인 함수들
def check_storage_connection():
    try:
        if not STORAGE_CONNECTION_STRING:
            return False
        blob_service_client = BlobServiceClient.from_connection_string(STORAGE_CONNECTION_STRING)
        # 간단한 연결 테스트
        list(blob_service_client.list_containers())
        return True
    except:
        return False

def check_search_connection():
    try:
        if not SEARCH_ENDPOINT or not SEARCH_KEY:
            return False
        search_client = SearchClient(
            endpoint=SEARCH_ENDPOINT,
            index_name=SEARCH_INDEX_NAME,
            credential=AzureKeyCredential(SEARCH_KEY)
        )
        return True
    except:
        return False

def check_openai_connection():
    try:
        if not OPENAI_API_KEY or not OPENAI_ENDPOINT:
            return False
        # 간단한 테스트 요청
        return True
    except:
        return False

# 메인 앱
st.title("🔍 ITO 이벤트 분석 agent")

# 탭 생성
tab1, tab2, tab3, tab4 = st.tabs(["📤 데이터 업로드", "📊 이벤트 분석", "📈 리포트", "⚙️ 설정"])

with tab1:
    st.header("이벤트 로그 업로드")
    
    # 파일 업로드
    uploaded_files = st.file_uploader(
        "CSV 파일들을 선택하세요",
        type=['csv'],
        accept_multiple_files=True,
        help="하나 또는 여러 개의 이벤트 로그 CSV 파일을 업로드할 수 있습니다"
    )
    
    if uploaded_files:
        st.success(f"✅ {len(uploaded_files)}개의 파일이 업로드되었습니다")
        
        # 각 파일 정보 및 미리보기
        file_data = {}
        total_events = 0
        all_hosts = set()
        
        for i, file in enumerate(uploaded_files):
            with st.expander(f"📄 {file.name}"):
                try:
                    # 인코딩 옵션 시도
                    encodings = ['utf-8', 'cp949', 'euc-kr', 'latin1']
                    df = None
                    
                    for encoding in encodings:
                        try:
                            file.seek(0)
                            df = pd.read_csv(file, encoding=encoding)
                            st.success(f"✅ {encoding} 인코딩으로 읽기 성공")
                            break
                        except UnicodeDecodeError:
                            continue
                        except Exception as e:
                            st.warning(f"⚠️ {encoding} 인코딩 실패: {str(e)}")
                    
                    if df is None:
                        st.error(f"❌ 파일을 읽을 수 없습니다. 지원되는 인코딩: {', '.join(encodings)}")
                        continue
                    
                    file.seek(0)  # 파일 포인터 리셋
                    
                    # 데이터 검증
                    st.write(f"📊 데이터 shape: {df.shape}")
                    st.write(f"📋 컬럼: {', '.join(df.columns.tolist())}")
                    
                    # 파일 정보 저장
                    file_data[file.name] = {
                        'data': df,
                        'size': file.size,
                        'events': len(df),
                        'hosts': df['Host'].unique().tolist() if 'Host' in df.columns else []
                    }
                    
                    # 전체 통계 업데이트
                    total_events += len(df)
                    if 'Host' in df.columns:
                        all_hosts.update(df['Host'].unique())
                    
                    # 파일별 정보 표시
                    col1, col2, col3, col4 = st.columns(4)
                    with col1:
                        st.metric("이벤트 수", f"{len(df):,}")
                    with col2:
                        st.metric("호스트 수", df['Host'].nunique() if 'Host' in df.columns else "N/A")
                    with col3:
                        st.metric("파일 크기", f"{file.size / 1024:.2f} KB")
                    with col4:
                        if 'Time' in df.columns:
                            try:
                                df['Time'] = pd.to_datetime(df['Time'])
                                period = f"{df['Time'].min().strftime('%m/%d')} ~ {df['Time'].max().strftime('%m/%d')}"
                                st.metric("기간", period)
                            except Exception as e:
                                st.metric("기간", "날짜 형식 오류")
                        else:
                            st.metric("기간", "N/A")
                    
                    # 데이터 미리보기
                    st.dataframe(df.head(5), use_container_width=True)
                    
                except Exception as e:
                    st.error(f"❌ 파일 읽기 오류: {str(e)}")
                    st.info("💡 CSV 파일 형식을 확인하세요:\n- 첫 줄은 헤더여야 합니다\n- 쉼표(,)로 구분되어야 합니다\n- UTF-8 또는 CP949 인코딩이어야 합니다")
        
        # 전체 요약 정보
        st.markdown("---")
        st.subheader("📊 전체 파일 요약")
        
        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric("총 파일 수", f"{len(uploaded_files)}개")
        with col2:
            st.metric("총 이벤트 수", f"{total_events:,}")
        with col3:
            st.metric("전체 호스트 수", f"{len(all_hosts)}개")
        
        # 데이터 저장 옵션
        st.markdown("---")
        st.subheader("💾 데이터 저장 옵션")
        
        save_option = st.radio(
            "데이터 저장 방식",
            ["개별 파일로 유지", "하나로 병합하여 저장"],
            horizontal=True,
            help="분석 시 사용할 데이터 저장 방식을 선택하세요"
        )
        
        if st.button("✅ 분석 준비 완료", type="primary", use_container_width=True):
            with st.spinner("데이터를 준비하는 중..."):
                try:
                    if save_option == "개별 파일로 유지":
                        # 각 파일을 개별적으로 세션에 저장
                        st.session_state['event_files'] = file_data
                        st.session_state['data_mode'] = 'multiple'
                        st.success(f"✅ {len(file_data)}개의 파일이 개별적으로 저장되었습니다!")
                        
                    else:  # 하나로 병합
                        # 디버그 정보 표시
                        st.write(f"📊 병합 대상 파일 수: {len(file_data)}")
                        for filename, data in file_data.items():
                            if 'data' in data:
                                st.write(f"  - {filename}: {len(data['data'])}행, 비어있음: {data['data'].empty}")
                        
                        # 모든 데이터프레임 병합
                        all_dfs = []
                        for filename, data in file_data.items():
                            if 'data' in data and not data['data'].empty:
                                all_dfs.append(data['data'])
                                st.success(f"✅ {filename} 추가됨")
                            else:
                                st.warning(f"⚠️ {filename} 파일이 비어있거나 잘못되었습니다.")
                        
                        if not all_dfs:
                            st.error("❌ 병합할 유효한 데이터가 없습니다. 파일을 확인해주세요.")
                            st.info("💡 파일이 비어있지 않은지, CSV 형식이 올바른지 확인하세요.")
                        else:
                            merged_df = pd.concat(all_dfs, ignore_index=True)
                            
                            # Time 컬럼이 있으면 정렬
                            if 'Time' in merged_df.columns:
                                merged_df['Time'] = pd.to_datetime(merged_df['Time'])
                                merged_df = merged_df.sort_values('Time')
                            
                            st.session_state['event_data'] = merged_df
                            st.session_state['data_mode'] = 'single'
                            st.success(f"✅ {len(all_dfs)}개의 파일이 병합되어 저장되었습니다! (총 {len(merged_df)}행)")
                    
                    # Azure Storage 업로드 (설정된 경우)
                    if STORAGE_CONNECTION_STRING and STORAGE_CONNECTION_STRING.strip():
                        try:
                            # 연결 문자열 검증
                            if "AccountName=" not in STORAGE_CONNECTION_STRING or "AccountKey=" not in STORAGE_CONNECTION_STRING:
                                st.warning("☁️ Storage 연결 문자열이 올바르지 않습니다. .env 파일을 확인하세요.")
                            else:
                                blob_service_client = BlobServiceClient.from_connection_string(STORAGE_CONNECTION_STRING)
                                container_name = "event-logs"
                                
                                # 컨테이너 존재 확인 및 생성
                                container_client = blob_service_client.get_container_client(container_name)
                                try:
                                    container_client.get_container_properties()
                                except:
                                    container_client.create_container()
                                    st.info(f"☁️ '{container_name}' 컨테이너를 생성했습니다.")
                                
                                # 파일 업로드
                                uploaded_count = 0
                                for filename, file_info in file_data.items():
                                    if 'data' in file_info and not file_info['data'].empty:
                                        blob_name = f"uploads/{datetime.now().strftime('%Y%m%d_%H%M%S')}_{filename}"
                                        blob_client = blob_service_client.get_blob_client(
                                            container=container_name,
                                            blob=blob_name
                                        )
                                        csv_data = file_info['data'].to_csv(index=False)
                                        blob_client.upload_blob(csv_data, overwrite=True)
                                        uploaded_count += 1
                                
                                if uploaded_count > 0:
                                    st.success(f"☁️ {uploaded_count}개 파일이 클라우드에 백업되었습니다")
                        except Exception as e:
                            st.warning(f"☁️ 클라우드 백업 실패: {str(e)}")
                            st.info("💡 .env 파일의 STORAGE_CONNECTION_STRING을 확인하세요")
                    
                    st.info("💡 이제 '이벤트 분석' 탭에서 분석을 시작할 수 있습니다!")
                    
                except Exception as e:
                    st.error(f"❌ 오류가 발생했습니다: {str(e)}")
                    st.info("💡 팁: CSV 파일이 올바른 형식인지 확인하세요. 헤더가 있고 데이터가 포함되어 있어야 합니다.")

with tab2:
    st.header("이벤트 분석")
    
    # 데이터 확인
    has_data = False
    if 'data_mode' in st.session_state:
        if st.session_state['data_mode'] == 'single' and 'event_data' in st.session_state:
            has_data = True
        elif st.session_state['data_mode'] == 'multiple' and 'event_files' in st.session_state:
            has_data = True
    elif 'event_data' in st.session_state:  # 기존 호환성
        has_data = True
        st.session_state['data_mode'] = 'single'
    
    if not has_data:
        st.warning("⚠️ 분석할 데이터가 없습니다.")
        st.info("👈 '데이터 업로드' 탭에서 CSV 파일을 업로드하거나, 아래에서 직접 업로드하세요.")
        
        # 여기서도 업로드 가능하도록 추가
        uploaded_file = st.file_uploader(
            "CSV 파일을 여기서 직접 업로드할 수도 있습니다",
            type=['csv'],
            key="tab2_uploader"
        )
        
        if uploaded_file is not None:
            df = pd.read_csv(uploaded_file)
            st.session_state['event_data'] = df
            st.session_state['data_mode'] = 'single'
            st.success("✅ 파일이 업로드되었습니다!")
            st.experimental_rerun()
    
    else:
        # 분석할 데이터 선택 (여러 파일인 경우)
        if st.session_state.get('data_mode') == 'multiple':
            st.subheader("📁 분석할 파일 선택")
            
            file_names = list(st.session_state['event_files'].keys())
            
            analysis_option = st.radio(
                "분석 방식",
                ["개별 파일 분석", "선택한 파일들 병합 분석", "전체 파일 병합 분석"],
                horizontal=True
            )
            
            if analysis_option == "개별 파일 분석":
                selected_file = st.selectbox("분석할 파일 선택", file_names)
                df = st.session_state['event_files'][selected_file]['data']
                st.info(f"📄 선택된 파일: {selected_file} ({len(df):,}개 이벤트)")
                
            elif analysis_option == "선택한 파일들 병합 분석":
                selected_files = st.multiselect("분석할 파일들 선택", file_names, default=file_names[:2] if len(file_names) >= 2 else file_names)
                if selected_files:
                    dfs = []
                    for f in selected_files:
                        if not st.session_state['event_files'][f]['data'].empty:
                            dfs.append(st.session_state['event_files'][f]['data'])
                    
                    if dfs:
                        df = pd.concat(dfs, ignore_index=True)
                        if 'Time' in df.columns:
                            df['Time'] = pd.to_datetime(df['Time'])
                            df = df.sort_values('Time')
                        st.info(f"📄 {len(selected_files)}개 파일 병합 ({len(df):,}개 이벤트)")
                    else:
                        st.warning("선택한 파일들에 유효한 데이터가 없습니다.")
                        df = pd.DataFrame()
                else:
                    st.warning("분석할 파일을 선택해주세요")
                    df = pd.DataFrame()  # 빈 데이터프레임 할당
                    
            else:  # 전체 파일 병합 분석
                dfs = []
                for data in st.session_state['event_files'].values():
                    if not data['data'].empty:
                        dfs.append(data['data'])
                
                if dfs:
                    df = pd.concat(dfs, ignore_index=True)
                    if 'Time' in df.columns:
                        df['Time'] = pd.to_datetime(df['Time'])
                        df = df.sort_values('Time')
                    st.info(f"📄 전체 {len(dfs)}개 파일 병합 ({len(df):,}개 이벤트)")
                else:
                    st.warning("유효한 데이터가 있는 파일이 없습니다.")
                    df = pd.DataFrame()
        
        else:  # single mode
            df = st.session_state['event_data']
        
        # 데이터가 비어있지 않은 경우에만 진행
        if not df.empty:
            # 데이터 요약 표시
            col1, col2, col3, col4 = st.columns(4)
            with col1:
                st.metric("총 이벤트", f"{len(df):,}")
            with col2:
                st.metric("호스트 수", df['Host'].nunique() if 'Host' in df.columns else 0)
            with col3:
                critical_count = len(df[df['Severity'].isin(['Disaster', 'High'])]) if 'Severity' in df.columns else 0
                st.metric("심각 이벤트", critical_count)
            with col4:
                st.metric("분석 준비", "✅ 완료")
            
            # 시간 범위 필터 추가
            if 'Time' in df.columns:
                st.markdown("---")
                st.subheader("⏰ 시간 범위 필터")
                
                df['Time'] = pd.to_datetime(df['Time'])
                min_date = df['Time'].min()
                max_date = df['Time'].max()
                
                col1, col2 = st.columns(2)
                with col1:
                    start_date = st.date_input(
                        "시작 날짜",
                        value=min_date.date(),
                        min_value=min_date.date(),
                        max_value=max_date.date()
                    )
                with col2:
                    end_date = st.date_input(
                        "종료 날짜",
                        value=max_date.date(),
                        min_value=min_date.date(),
                        max_value=max_date.date()
                    )
                
                # 날짜 필터 적용
                if start_date and end_date:
                    mask = (df['Time'].dt.date >= start_date) & (df['Time'].dt.date <= end_date)
                    filtered_df = df[mask]
                    
                    if len(filtered_df) < len(df):
                        st.info(f"🔍 필터 적용: {len(filtered_df):,}개 이벤트 (전체 {len(df):,}개 중)")
                        df = filtered_df
            
            # 간략화된 AI 분석 섹션
            st.markdown("---")
            st.subheader("🤖 AI 기반 빠른 분석")
            
            col1, col2 = st.columns([3, 1])
            with col1:
                st.info("💡 AI가 현재 상태를 빠르게 진단하고 주요 문제점과 조치사항을 알려드립니다.")
            with col2:
                if st.button("🚀 AI 분석 실행", type="primary", use_container_width=True):
                    with st.spinner("AI가 데이터를 분석하는 중..."):
                        ai_result = perform_simple_ai_analysis(df)
                        
                        # 결과를 세션에 저장
                        st.session_state['ai_analysis_result'] = ai_result
                        st.session_state['ai_analysis_time'] = datetime.now()
            
            # AI 분석 결과 표시
            if 'ai_analysis_result' in st.session_state:
                st.markdown("### 📋 AI 분석 결과")
                
                # 분석 시간 표시
                if 'ai_analysis_time' in st.session_state:
                    time_diff = datetime.now() - st.session_state['ai_analysis_time']
                    minutes_ago = int(time_diff.total_seconds() / 60)
                    if minutes_ago < 1:
                        time_text = "방금 전"
                    else:
                        time_text = f"{minutes_ago}분 전"
                    st.caption(f"🕐 분석 시간: {time_text}")
                
                # AI 결과를 보기 좋게 표시
                with st.container():
                    # 결과를 마크다운으로 표시
                    st.markdown(st.session_state['ai_analysis_result'])
                    
                    # 분석 결과 요약 메트릭 (AI 응답에서 추출 가능한 경우)
                    st.markdown("---")
                    
                    # 다운로드 옵션
                    col1, col2, col3 = st.columns([1, 1, 2])
                    with col1:
                        st.download_button(
                            label="📥 분석 결과 저장 (TXT)",
                            data=st.session_state['ai_analysis_result'],
                            file_name=f"ai_analysis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
                            mime="text/plain"
                        )
                    
                    with col2:
                        # 마크다운 형식으로도 저장
                        markdown_content = f"""# AI 이벤트 분석 리포트
                        
생성일시: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

---

{st.session_state['ai_analysis_result']}

---

본 리포트는 AI 기반 자동 분석으로 생성되었습니다.
"""
                        st.download_button(
                            label="📥 분석 결과 저장 (MD)",
                            data=markdown_content,
                            file_name=f"ai_analysis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md",
                            mime="text/markdown"
                        )
                
                # 추가 분석 옵션
                with st.expander("🔍 추가 분석 옵션"):
                    st.info("더 깊은 분석이 필요하신가요?")
                    
                    col1, col2 = st.columns(2)
                    with col1:
                        if st.button("🔄 다시 분석", use_container_width=True):
                            with st.spinner("AI가 데이터를 다시 분석하는 중..."):
                                ai_result = perform_simple_ai_analysis(df)
                                st.session_state['ai_analysis_result'] = ai_result
                                st.session_state['ai_analysis_time'] = datetime.now()
                                st.rerun()
                    
                    with col2:
                        if st.button("📊 통계 리포트 생성", use_container_width=True):
                            st.info("'리포트' 탭으로 이동하여 상세 통계를 확인하세요.")
            
            st.markdown("---")
            
            # 통계 그래프 섹션
            st.subheader("📈 이벤트 통계 분석")
            
            # 그래프 탭 생성
            graph_tabs = st.tabs(["시간대별 분석", "호스트별 분석", "심각도 분석", "트렌드 분석"])
            
            with graph_tabs[0]:
                # 시간대별 분석
                if 'Time' in df.columns:
                    col1, col2 = st.columns(2)
                    
                    with col1:
                        # 시간별 이벤트 발생 수
                        hourly_counts = df.groupby(df['Time'].dt.hour).size()
                        
                        # 0-23시까지 모든 시간 포함
                        all_hours = pd.Series(0, index=range(24))
                        all_hours.update(hourly_counts)
                        
                        fig = px.bar(
                            x=all_hours.index,
                            y=all_hours.values,
                            title='시간대별 이벤트 발생 현황',
                            labels={'x': '시간', 'y': '이벤트 수'},
                            color=all_hours.values,
                            color_continuous_scale='Blues',
                            text=all_hours.values  # 막대 위에 값 표시
                        )
                        
                        # x축 설정 - 1시간 단위로 표시
                        fig.update_xaxes(
                            tickmode='linear',
                            tick0=0,
                            dtick=1,
                            tickformat='%d',
                            ticksuffix='시',
                            range=[-0.5, 23.5],
                            title="시간 (0시 ~ 23시)"
                        )
                        
                        # 막대 위 텍스트 표시 설정
                        fig.update_traces(texttemplate='%{text}', textposition='outside')
                        
                        fig.update_layout(
                            showlegend=False,
                            yaxis=dict(title='이벤트 수'),
                            bargap=0.2
                        )
                        st.plotly_chart(fig, use_container_width=True)
                    
                    with col2:
                        # 요일별 이벤트 발생 수
                        df_copy = df.copy()
                        df_copy['DayOfWeek'] = df_copy['Time'].dt.day_name()
                        day_order = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
                        daily_counts = df_copy['DayOfWeek'].value_counts().reindex(day_order, fill_value=0)
                        
                        fig = px.line(
                            x=daily_counts.index,
                            y=daily_counts.values,
                            title='요일별 이벤트 발생 추이',
                            labels={'x': '요일', 'y': '이벤트 수'},
                            markers=True
                        )
                        st.plotly_chart(fig, use_container_width=True)
                    
                    # 시계열 히트맵
                    df_heatmap = df.copy()
                    df_heatmap['Date'] = df_heatmap['Time'].dt.date
                    df_heatmap['Hour'] = df_heatmap['Time'].dt.hour
                    heatmap_data = df_heatmap.groupby(['Date', 'Hour']).size().unstack(fill_value=0)
                    
                    # 모든 시간(0-23)이 포함되도록 보장
                    for hour in range(24):
                        if hour not in heatmap_data.columns:
                            heatmap_data[hour] = 0
                    heatmap_data = heatmap_data.reindex(columns=range(24), fill_value=0)
                    
                    fig = px.imshow(
                        heatmap_data.T,
                        title='일별/시간별 이벤트 히트맵',
                        labels=dict(x="날짜", y="시간", color="이벤트 수"),
                        aspect="auto",
                        color_continuous_scale='Reds'
                    )
                    
                    # y축을 시간 형식으로 표시
                    fig.update_yaxes(
                        tickmode='array',
                        tickvals=list(range(24)),
                        ticktext=[f'{i:02d}:00' for i in range(24)],
                        title="시간 (24시간 형식)"
                    )
                    
                    # x축 날짜 형식 개선
                    fig.update_xaxes(
                        tickformat='%m/%d',
                        title="날짜"
                    )
                    
                    st.plotly_chart(fig, use_container_width=True)
            
            with graph_tabs[1]:
                # 호스트별 분석
                if 'Host' in df.columns:
                    col1, col2 = st.columns(2)
                    
                    with col1:
                        # Top 10 호스트
                        top_hosts = df['Host'].value_counts().head(10)
                        fig = px.bar(
                            y=top_hosts.index,
                            x=top_hosts.values,
                            orientation='h',
                            title='Top 10 이벤트 발생 호스트',
                            labels={'x': '이벤트 수', 'y': '호스트'},
                            color=top_hosts.values,
                            color_continuous_scale='Reds'
                        )
                        fig.update_layout(showlegend=False)
                        st.plotly_chart(fig, use_container_width=True)
                    
                    with col2:
                        # 호스트별 심각도 분포
                        if 'Severity' in df.columns:
                            host_severity = pd.crosstab(df['Host'], df['Severity'])
                            top_hosts_list = df['Host'].value_counts().head(5).index
                            
                            fig = go.Figure()
                            for severity in ['Disaster', 'High', 'Average', 'Warning', 'Information', 'Not classified']:
                                if severity in host_severity.columns:
                                    fig.add_trace(go.Bar(
                                        name=severity,
                                        x=host_severity.loc[top_hosts_list, severity].values if severity in host_severity.columns else [0]*5,
                                        y=top_hosts_list,
                                        orientation='h'
                                    ))
                            
                            fig.update_layout(
                                barmode='stack',
                                title='Top 5 호스트의 심각도별 이벤트 분포',
                                xaxis_title='이벤트 수',
                                yaxis_title='호스트'
                            )
                            st.plotly_chart(fig, use_container_width=True)
            
            with graph_tabs[2]:
                # 심각도 분석 - 다양한 컬럼명 지원
                severity_col = None
                possible_severity_cols = ['Severity', 'severity', 'SEVERITY', '심각도', 'Level', 'level']
                
                for col in possible_severity_cols:
                    if col in df.columns:
                        severity_col = col
                        break
                
                if severity_col:
                    st.markdown("### 🎯 Severity 분석")
                    
                    # 디버깅 정보
                    st.caption(f"분석 컬럼: {severity_col}")
                    
                    # 심각도별 분포 계산 (대소문자 구분 없이)
                    df_severity = df.copy()
                    df_severity[severity_col] = df_severity[severity_col].astype(str).str.lower().str.strip()
                    severity_counts = df_severity[severity_col].value_counts()
                    total_events = len(df)
                    
                    # 고유 값 확인
                    st.caption(f"발견된 Severity 값: {', '.join(severity_counts.index.tolist())}")
                    
                    # 색상 매핑 (대소문자 구분 없이)
                    colors = {
                        'fatal': '#E45959',
                        'critical': '#E97659', 
                        'major': '#FFA059',
                        'information': '#7499FF',
                        'info': '#7499FF',
                        'warning': '#FFC859',
                        'error': '#E45959',
                        'high': '#E97659',
                        'medium': '#FFA059',
                        'low': '#7499FF'
                    }
                    
                    # Severity 순서 정의 (위험도 순)
                    severity_order = ['fatal', 'critical', 'major', 'information']
                    
                    # 데이터 준비
                    severity_data = []
                    for severity in severity_counts.index:
                        count = severity_counts[severity]
                        percentage = (count / total_events * 100)
                        severity_data.append({
                            'Severity': severity,
                            'Count': count,
                            'Percentage': percentage
                        })
                    
                    if severity_data:
                        severity_df = pd.DataFrame(severity_data)
                        
                        # 1. 도넛 차트
                        fig = go.Figure(data=[go.Pie(
                            labels=[s['Severity'].upper() for s in severity_data],
                            values=[s['Count'] for s in severity_data],
                            hole=0.5,
                            marker=dict(
                                colors=[colors.get(s['Severity'], '#999999') for s in severity_data],
                                line=dict(color='white', width=2)
                            ),
                            texttemplate='<b>%{label}</b><br>%{value:,}<br>(%{percent})',
                            textposition='outside',
                            textfont=dict(size=16),
                            hovertemplate='<b>%{label}</b><br>' +
                                         '이벤트 수: %{value:,}건<br>' +
                                         '비율: %{percent}<br>' +
                                         '<extra></extra>',
                            sort=False
                        )])
                        
                        # 중앙 정보
                        fig.add_annotation(
                            text=f'<b>전체 이벤트</b><br>{total_events:,}건',
                            x=0.5, y=0.5,
                            font=dict(size=20),
                            showarrow=False
                        )
                        
                        fig.update_layout(
                            title='Severity 분포',
                            height=500,
                            showlegend=True,
                            legend=dict(
                                orientation="v",
                                yanchor="middle",
                                y=0.5,
                                xanchor="left",
                                x=1.05
                            )
                        )
                        
                        st.plotly_chart(fig, use_container_width=True)
                        
                        # 2. Severity별 메트릭
                        st.markdown("#### 📊 Severity별 현황")
                        
                        # 실제 존재하는 severity만 표시
                        cols = st.columns(min(len(severity_counts), 4))
                        for idx, (severity, count) in enumerate(severity_counts.items()):
                            if idx < len(cols):
                                percentage = (count / total_events * 100) if total_events > 0 else 0
                                
                                with cols[idx]:
                                    # 색상과 아이콘
                                    color = colors.get(severity, '#999999')
                                    if severity in ['fatal', 'error']:
                                        icon = "🚨"
                                    elif severity in ['critical', 'high']:
                                        icon = "⚠️"
                                    elif severity in ['major', 'medium', 'warning']:
                                        icon = "📌"
                                    else:
                                        icon = "ℹ️"
                                    
                                    st.metric(
                                        f"{icon} {severity.upper()}",
                                        f"{count:,}",
                                        f"{percentage:.1f}%"
                                    )
                        
                        # 3. 상세 테이블
                        st.markdown("#### 📋 Severity 상세 분석")
                        
                        # 테이블 데이터 생성
                        table_data = []
                        cumulative_pct = 0
                        
                        for severity, count in severity_counts.items():
                            percentage = (count / total_events * 100)
                            cumulative_pct += percentage
                            
                            table_data.append({
                                'Severity': severity.upper(),
                                '이벤트 수': f"{count:,}",
                                '비율(%)': f"{percentage:.2f}%",
                                '누적 비율(%)': f"{cumulative_pct:.2f}%"
                            })
                        
                        if table_data:
                            table_df = pd.DataFrame(table_data)
                            st.dataframe(table_df, use_container_width=True, hide_index=True)
                        
                        # 4. 주요 인사이트
                        st.markdown("#### 💡 분석 결과")
                        
                        # 위험 레벨 계산 (다양한 이름 지원)
                        high_risk_keywords = ['fatal', 'critical', 'error', 'high', 'disaster']
                        medium_risk_keywords = ['major', 'warning', 'medium', 'average']
                        
                        high_risk_count = sum(severity_counts.get(k, 0) for k in high_risk_keywords if k in severity_counts.index)
                        high_risk_pct = (high_risk_count / total_events * 100) if total_events > 0 else 0
                        
                        if high_risk_pct > 20:
                            st.error(f"⚠️ 고위험 이벤트가 {high_risk_pct:.1f}%로 매우 높습니다. 즉각적인 대응이 필요합니다.")
                        elif high_risk_pct > 10:
                            st.warning(f"📌 고위험 이벤트가 {high_risk_pct:.1f}%입니다. 주의가 필요합니다.")
                        else:
                            st.success(f"✅ 고위험 이벤트가 {high_risk_pct:.1f}%로 안정적입니다.")
                        
                        # 가장 많은 severity
                        if len(severity_counts) > 0:
                            top_severity = severity_counts.index[0]
                            top_pct = (severity_counts.iloc[0] / total_events * 100)
                            st.info(f"📊 가장 많은 이벤트: **{top_severity.upper()}** ({top_pct:.1f}%)")
                    
                    else:
                        st.warning("Severity 데이터가 비어있습니다.")
                else:
                    st.warning("❌ Severity 컬럼을 찾을 수 없습니다.")
                    st.info(f"사용 가능한 컬럼: {', '.join(df.columns.tolist())}")
            
            with graph_tabs[3]:
                # 트렌드 분석
                if 'Time' in df.columns:
                    # 일별 이벤트 트렌드
                    daily_events = df.groupby(df['Time'].dt.date).size()
                    
                    fig = go.Figure()
                    fig.add_trace(go.Scatter(
                        x=daily_events.index,
                        y=daily_events.values,
                        mode='lines+markers',
                        name='일별 이벤트',
                        line=dict(color='blue', width=2),
                        marker=dict(size=6)
                    ))
                    
                    # 이동 평균 추가
                    ma7 = daily_events.rolling(window=7, min_periods=1).mean()
                    fig.add_trace(go.Scatter(
                        x=ma7.index,
                        y=ma7.values,
                        mode='lines',
                        name='7일 이동평균',
                        line=dict(color='red', width=2, dash='dash')
                    ))
                    
                    fig.update_layout(
                        title='일별 이벤트 발생 트렌드',
                        xaxis_title='날짜',
                        yaxis_title='이벤트 수',
                        hovermode='x unified'
                    )
                    
                    # x축 날짜 형식 설정
                    fig.update_xaxes(
                        tickformat='%Y-%m-%d',
                        tickangle=45,
                        dtick='D1'  # 1일 단위로 표시
                    )
                    
                    st.plotly_chart(fig, use_container_width=True)
                    
                    # 심각도별 트렌드
                    if 'Severity' in df.columns:
                        severity_trend = df.groupby([df['Time'].dt.date, 'Severity']).size().unstack(fill_value=0)
                        
                        fig = go.Figure()
                        for severity in ['Disaster', 'High', 'Average', 'Warning']:
                            if severity in severity_trend.columns:
                                fig.add_trace(go.Scatter(
                                    x=severity_trend.index,
                                    y=severity_trend[severity],
                                    mode='lines',
                                    name=severity,
                                    stackgroup='one'
                                ))
                        
                        fig.update_layout(
                            title='심각도별 이벤트 발생 트렌드',
                            xaxis_title='날짜',
                            yaxis_title='이벤트 수',
                            hovermode='x unified'
                        )
                        st.plotly_chart(fig, use_container_width=True)

with tab3:
    st.header("트렌드 및 통계 리포트")
    
    # 데이터 확인
    if 'event_data' not in st.session_state and 'event_files' not in st.session_state:
        st.warning("⚠️ 리포트를 생성할 데이터가 없습니다.")
        st.info("👈 '데이터 업로드' 탭에서 CSV 파일을 업로드하세요.")
    else:
        st.markdown("### 📊 종합 리포트 생성")
        st.info("현재 업로드된 데이터를 기반으로 종합 통계 리포트를 생성합니다.")
        
        if st.button("📥 종합 리포트 생성", type="primary", use_container_width=True):
            generate_report("종합 리포트")

with tab4:
    st.header("시스템 설정")
    
    # 연결 상태 확인
    st.subheader("연결 상태")
    
    col1, col2, col3 = st.columns(3)
    
    with col1:
        storage_status = check_storage_connection()
        if storage_status:
            st.success("✅ Storage Account 연결됨")
        else:
            st.error("❌ Storage Account 연결 실패")
    
    with col2:
        search_status = check_search_connection()
        if search_status:
            st.success("✅ AI Search 연결됨")
        else:
            st.error("❌ AI Search 연결 실패")
    
    with col3:
        openai_status = check_openai_connection()
        if openai_status:
            st.success("✅ OpenAI 연결됨")
        else:
            st.error("❌ OpenAI 연결 실패")
    
    # 환경 변수 설정 가이드
    if not all([storage_status, search_status, openai_status]):
        st.warning("⚠️ 일부 서비스가 연결되지 않았습니다.")
        st.markdown("""
        ### 환경 변수 설정 가이드
        
        `.env` 파일을 생성하고 다음 정보를 입력하세요:
        
        ```
        STORAGE_CONNECTION_STRING="your_storage_connection_string"
        SEARCH_ENDPOINT="https://your-search-service.search.windows.net"
        SEARCH_KEY="your_search_api_key"
        OPENAI_API_KEY="your_openai_api_key"
        OPENAI_ENDPOINT="https://your-openai-resource.openai.azure.com/"
        OPENAI_DEPLOYMENT="gpt-4o-deployment"
        ```
        """)

# 사이드바
st.sidebar.markdown("### ITO 이벤트 분석 agent")

st.sidebar.markdown("---")
st.sidebar.markdown("v1.1.1")

if __name__ == "__main__":
    pass