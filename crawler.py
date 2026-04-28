import os
import requests
import time
import random
import pandas as pd
from datetime import datetime, timezone
from supabase import create_client, Client
from dotenv import load_dotenv

# ==========================================
# ⚙️ 1. 통합 환경 설정
# ==========================================
TARGET_PLATES = [
    {"name": "공식 뉴스", "plate_id": 43, "plate_unique_id": "official", "storage": "DB"},
    {"name": "유저 공략", "plate_id": 45, "plate_unique_id": "guides", "storage": "DB"},
    {"name": "전초기지", "plate_id": 38, "plate_unique_id": "outpost", "storage": "DB"},
    {"name": "니케 아트", "plate_id": 39, "plate_unique_id": "nikkeart", "storage": "CSV"}
]
# 니케 아트 탭 전용 설정
SAVE_DIR = "./nikke_arts"
os.makedirs(SAVE_DIR, exist_ok=True)
CSV_PATH = os.path.join(SAVE_DIR, "metadata.csv")

# .env 파일 안의 내용들을 읽어와서 컴퓨터 환경 변수로.
load_dotenv()

# 환경 변수에서 키값을 꺼내기.
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# DB 접속
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# API 설정
HEADERS = {
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "x-channel-type": "2",
    "x-language": "ko",
    "x-common-params": '{"game_id":"16","area_id":"global","source":"pc_web","intl_game_id":"29080","language":"ko","env":"prod","data_statistics_client_type":"pc_web"}',
    "content-type": "application/json"
}

URL_POST_LIST = "https://api.blablalink.com/api/ugc/direct/standalonesite/Dynamics/GetPostList"
URL_COMMENTS = "https://api.blablalink.com/api/ugc/direct/standalonesite/Dynamics/GetPostCommentsV2"
URL_BATCH_REPLIES = "https://api.blablalink.com/api/ugc/direct/standalonesite/Dynamics/BatchGetPostCommentReplies"

# ==========================================
# 🛠️ 2. 통합 데이터 정제 함수
# ==========================================
def parse_unified_data(raw_json, plate_name):
    raw_tags = raw_json.get("tags") or []
    tag_names = [t.get("name") for t in raw_tags if isinstance(t, dict)]
    
    # 텍스트 정제 (줄바꿈 제거)
    summary = str(raw_json.get("content_summary", "")).replace('\n', ' ').strip()
    
    return {
        "post_uuid": str(raw_json.get("post_uuid")),
        "plate_name": plate_name, # 어떤 게시판인지 구분자 추가
        "title": str(raw_json.get("title", "")).strip(),
        "content_text": summary,
        "tags": tag_names if plate_name != "아트 탭" else ", ".join(tag_names),
        "author_id": raw_json.get("user", {}).get("intl_openid"),
        
        # 지표 통합 (공유 수 forward_count 포함)
        "browse_count": int(raw_json.get("browse_count") or 0),
        "upvote_count": int(raw_json.get("upvote_count") or 0),
        "collection_count": int(raw_json.get("collection_count") or 0),
        "ai_flag": int(raw_json.get("ai_content_type") or 0),
        "is_original": bool(raw_json.get("is_original_content") or False),
        "comment_count": int(raw_json.get("comment_count") or 0),
        "pic_click_count": int(raw_json.get("pic_click_count") or 0),
        "forward_count": int(raw_json.get("forward_count") or 0),
        
        "is_official": bool(raw_json.get("is_official") or False),
        "created_at": raw_json.get("created_on"),
        "image_urls": raw_json.get("pic_urls") or []
    }

def load_post(parsed_post: dict):
    try:
        supabase.table("posts").upsert(parsed_post).execute()
        return True
    except Exception as e:
        print(f"[게시글 적재 실패] {parsed_post.get('title')[:15]}... ({e})")
        return False

def load_comments(flat_comments: list, post_uuid: str):
    parents, replies = [], []

    # collected_time = datetime.now(timezone.utc).isoformat() 
    
    for comment in flat_comments:
        db_record = {
            "comment_uuid": comment.get("uuid"),
            "post_uuid": post_uuid,
            "parent_id": comment.get("parent_id"), 
            "content": comment.get("content"),
            "upvote_count": comment.get("upvote", 0),
            
            "is_author": comment.get("is_author"),
            "created_at": comment.get("created_at"),
            # "collected_at": collected_time 
        }
        if db_record["parent_id"] is None: parents.append(db_record)
        else: replies.append(db_record)
            
    try:
        if parents: supabase.table("comments").upsert(parents).execute()
        if replies: supabase.table("comments").upsert(replies).execute()
        print(f"[댓글 적재 완료] 부모 {len(parents)}개, 대댓글 {len(replies)}개")
    except Exception as e:
        print(f"[댓글 적재 실패] {e}")

def download_images(parsed_art):
    post_uuid = parsed_art["post_uuid"]
    urls = parsed_art["image_urls"]
    saved_file_paths = [] 
    
    for idx, url in enumerate(urls):
        try:
            if ".gif" in url.lower(): continue
            filename = f"{post_uuid}_{idx}.jpg"
            filepath = os.path.join(SAVE_DIR, filename)
            
            if os.path.exists(filepath):
                saved_file_paths.append(filepath)
                continue
                
            res = requests.get(url, stream=True, timeout=10)
            if res.status_code == 200:
                with open(filepath, 'wb') as f:
                    for chunk in res.iter_content(1024):
                        f.write(chunk)
                saved_file_paths.append(filepath)
            time.sleep(0.3)
        except Exception as e:
            print(f"[이미지 다운로드 실패] {url} - 사유: {e}")
    return saved_file_paths

# ==========================================
# 🛠️ 3. 증분 수집을 위한 DB 사전 조회
# ==========================================
def get_existing_uuids(plate_name):
    """DB에서 '해당 게시판'에 속한 모든 게시글 ID를 가져와 Set으로 반환합니다."""
    print(f"🔍 DB에서 [{plate_name}]의 기존 수집된 ID를 불러옵니다...")
    try:
        # 💡 시니어의 수정: plate_name으로 필터링하고, limit을 과감히 없애거나 아주 넉넉하게(10만 등) 줍니다.
        res = supabase.table("posts").select("post_uuid").eq("plate_name", plate_name).limit(100000).execute()
        existing_uuids = set(item['post_uuid'] for item in res.data)
        print(f"{len(existing_uuids)}개의 기존 ID를 메모리에 로드했\n")
        return existing_uuids
    except Exception as e:
        print(f"기존 ID 불러오기 실패 (초기 수집으로 간주): {e}\n")
        return set()

def get_existing_art_uuids():
    """CSV 장부에서 아트 탭 ID를 불러옵니다 (누락 복구)"""
    if os.path.exists(CSV_PATH):
        try:
            df = pd.read_csv(CSV_PATH)
            existing_uuids = set(df['post_uuid'].astype(str))
            print(f"아트 탭 기존 장부 발견 {len(existing_uuids)}개의 기록을 메모리에 올렸습니다.")
            return existing_uuids
        except Exception as e:
            print(f"CSV 장부 읽기 실패: {e}")
    return set()

# ==========================================
# 🛠️ 3-2. 댓글 수집
# ==========================================
def fetch_all_comments_for_post(post_uuid):
    is_finish = False
    current_cursor = ""
    flat_comments = []
    parent_uuids = [] 
    
    # 4-1. 부모 댓글 수집 (수정)
    while not is_finish:
        payload = {"post_uuid": post_uuid, "limit": 20, "page_type": 0, "next_page_cursor": current_cursor, "order_by": 2}
        try:
            res = requests.post(URL_COMMENTS, headers=HEADERS, json=payload, timeout=10)
            if res.status_code != 200: break
            data = res.json() # 여기서 JSON 파싱 에러가 나도 except로 빠짐
        except Exception as e:
            print(f"[댓글 네트워크 에러] {e} - 수집을 중단합니다.")
            break # 에러가 나면 스크립트를 죽이지 않고 해당 댓글 수집만 포기함
        try:
            for c in data['data']['list']:
                parent_uuids.append(c['comment_uuid'])
                flat_comments.append({
                    "uuid": c['comment_uuid'], 
                    "parent_id": None,
                    "content": c.get('content', ''), # HTML 태그 포함 원본
                    "upvote": c.get('upvote_count', 0),
                    # 작성자 여부와 작성 시간 추출
                    "is_author": c.get('is_author', False), 
                    "created_at": c.get('created_on') # API에 따라 'created_on' 또는 'created_at' 확인 필요
                })
            
            page_info = data['data']['page_info']
            is_finish = page_info['is_finish']
            current_cursor = page_info['next_page_cursor']
        except KeyError: break
        time.sleep(random.uniform(0.5, 1.0))
        
    # 4-2. 대댓글 수집 (배치 처리)
    if parent_uuids:
        for i in range(0, len(parent_uuids), 10):
            batch_target = parent_uuids[i:i+10]
            batch_payload = {"comment_uuids": batch_target, "limit": 10, "order_by": 2}

            try:
                res = requests.post(URL_BATCH_REPLIES, headers=HEADERS, json=batch_payload, timeout=10)
                if res.status_code == 200:
                    replies_map = res.json().get('data', {}).get('replies_map', {})
                    # ... (이하 기존 파싱 로직 동일) ...
                    for p_uuid, parent_data in replies_map.items():
                        replies = parent_data.get('data_list', []) 
                        for r in replies:
                            flat_comments.append({
                                "uuid": r.get('comment_uuid'), 
                                "parent_id": p_uuid,
                                "content": r.get('content', ''), 
                                "upvote": r.get('upvote_count', 0),
                                # 💡 추가됨: 대댓글의 작성자 여부와 작성 시간
                                "is_author": r.get('is_author', False),
                                "created_at": r.get('created_on')
                            })
            except Exception as e:
                print(f"    ⚠️ [대댓글 네트워크 에러] {e}")
                pass # 대댓글 하나 실패해도 멈추지 않고 다음 배치로 넘어감
            time.sleep(0.5)
            
    return flat_comments
        
# ==========================================
# 🚀 4. 메인 오케스트레이션 (V4)
# ==========================================
def run_v4_pipeline():
    print(f"🚀 [V4 통합 파이프라인] {datetime.now().strftime('%Y-%m-%d %H:%M')} 가동 시작\n")
    
    for plate in TARGET_PLATES:
        print(f"{'='*50}")
        print(f"▶️ [{plate['name']}] 수집 준비 중... (Storage: {plate['storage']})")
        
        # 1. 모드 설정 (전초기지만 과거 데이터 끝까지 파헤치기 / 04-20 모든 게시판 업데이트모드)
        if plate['name'] in []:
            CRAWL_MODE = "HISTORY"
        else:
            CRAWL_MODE = "UPDATE"
            
        # 2. Storage에 따른 과거 기록(장부) 로드 및 초기화
        if plate['storage'] == "DB":
            existing_uuids = get_existing_uuids(plate['name'])
        else:
            existing_uuids = get_existing_art_uuids()
            new_metadata_records = []
            
        is_finish = False
        current_cursor = ""
        
        # 트래픽 볼륨에 따른 동적 페이지 할당
        if plate['name'] == "전초기지":
            max_pages = 1000
        else:
            max_pages = 100   
            
        page_count = 0
        total_loaded_posts = 0
        consecutive_duplicates = 0
        MAX_TOLERANCE = 5 

        # 이번 수집 런에서 본 글들을 기억하는 '단기 기억 장부'
        session_seen_uuids = set()
        
        while not is_finish and page_count < max_pages:
            page_count += 1
            # 최신순 -> order_by:1
            payload = {"search_type": 0, "plate_id": plate['plate_id'], "plate_unique_id": plate['plate_unique_id'], "order_by": 1, "limit": "10", "regions": ["all"]}
            if current_cursor: payload["nextPageCursor"] = current_cursor
                
            print(f"\n  [진행상황] {plate['name']} {page_count}페이지 요청 중...")
            
            try:
                res = requests.post(URL_POST_LIST, headers=HEADERS, json=payload, timeout=10)
                if res.status_code != 200:
                    print(f"❌ 서버 상태 이상: {res.status_code}")
                    break
                data = res.json()
            except Exception as e:
                print(f"❌ [네트워크/파싱 치명적 에러] {e} - 해당 게시판 수집을 일시 중단합니다.")
                break # 에러 시 스크립트가 죽지 않고, 바깥 for문으로 빠져나가 '다음 게시판(plate)' 수집을 시작함

            post_list = data.get('data', {}).get('list', [])

            # 🛑 [데이터 기반 무한루프 방어] 
            # 이번 페이지에서 가져온 10개의 UUID를 추출합니다.
            current_page_uuids = [str(p.get("post_uuid")) for p in post_list]

            # 가져온 10개가 모두 '단기 기억 장부'에 이미 있는 것들이라면? -> 서버가 루프에 빠진 것입니다!
            if current_page_uuids and all(uid in session_seen_uuids for uid in current_page_uuids):
                print(f"  🚨 [서버 한계 도달] API가 방금 전에 준 페이지를 또 주며 거짓말을 합니다. 강제 탈출합니다!")
                break
            # 무사히 통과했다면, 단기 기억 장부에 이번 페이지 UUID들을 추가합니다.
            session_seen_uuids.update(current_page_uuids)
            
            for raw_post in post_list:
                parsed_post = parse_unified_data(raw_post, plate['name'])
                post_uuid = parsed_post['post_uuid']
                
                # 🛑 증분 수집 & 발굴 로직
                if post_uuid in existing_uuids:
                    if CRAWL_MODE == "UPDATE":
                        consecutive_duplicates += 1
                        print(f"    ⏩ [최신] 이미 장부에 있습니다. ({consecutive_duplicates}/{MAX_TOLERANCE})")
                        if consecutive_duplicates >= MAX_TOLERANCE:
                            print(f"    🚨 [조기 종료] {plate['name']} 수집을 마칩니다.")
                            is_finish = True # 💡 return 대신 break를 유도!
                            break 
                        continue
                        
                    elif CRAWL_MODE == "HISTORY":
                        # DB인 경우에만 덮어쓰기 진행 (CSV는 마지막에 Pandas가 일괄 병합함)
                        if plate['storage'] == "DB":
                            print(f"    🔄 [지표 업데이트] 기존 글에 새로운 데이터를 덮어씁니다.")
                            load_post(parsed_post)
                        else:
                            print(f"    ⏩ [발굴] 이미 CSV 장부에 있습니다. 스킵!")
                        continue 
                else:
                    consecutive_duplicates = 0
           
                    # ==========================================
                    # 🔀 [핵심] Storage 분기 처리 로직
                    # ==========================================
                    if plate['storage'] == "DB":
                        # DB 적재 (기존 로직)
                        if load_post(parsed_post):
                            total_loaded_posts += 1
                            # fetch_all_comments_for_post 함수가 정의되어 있다고 가정
                            flat_comments = fetch_all_comments_for_post(post_uuid) 
                            if flat_comments:
                                load_comments(flat_comments, post_uuid)
                                
                    # elif plate['storage'] == "CSV":
                    #     # 로컬 이미지 다운로드 및 CSV 기록 (아트 탭 전용)
                    #     if parsed_post["image_urls"]:
                    #         downloaded_paths = download_images(parsed_post)
                    #         if downloaded_paths:
                    #             total_loaded_posts += len(downloaded_paths)
                    #             for path in downloaded_paths:
                    #                 record = parsed_post.copy()
                    #                 del record['image_urls']
                    #                 record['filename'] = os.path.basename(path)
                    #                 new_metadata_records.append(record)

                    # [04-28] 추후 DB에 적재하기 위해 변경 
                    elif plate['storage'] == "CSV":
                        # 클라우드 최적화: 18GB 다운로드 삭제, URL 파이프(|) 결합 저장
                        record = parsed_post.copy()
                        
                        if record.get("image_urls"):
                            # 리스트 형태의 URL들을 파이프(|)로 묶어서 하나의 문자열로 만듭니다.
                            record['image_url'] = "|".join(record['image_urls'])
                        else:
                            record['image_url'] = None
                            
                        # CSV에는 리스트 객체를 바로 넣을 수 없으므로 원본 리스트는 삭제합니다.
                        del record['image_urls'] 
                        
                        # 파일명은 이제 필요 없으니 삭제.
                        if 'filename' in record: del record['filename']
                        
                        new_metadata_records.append(record)

            if is_finish: break # While 루프 완전 탈출
            
            try:
                page_info = data['data']['page_info']
                is_finish = page_info['is_finish']
                
                # 무한루프 방어
                next_cursor = page_info['next_page_cursor']
                
                if current_cursor != "" and current_cursor == next_cursor:
                    print(f"[서버 버그 감지] 커서가 갱신되지 않습니다. ({current_cursor}) 강제 탈출")
                    break
                    
                current_cursor = next_cursor
                
            except KeyError: break
            
            time.sleep(random.uniform(1.5, 3.0))

        # ==========================================
        # 💾 CSV 최종 병합 로직 (해당 게시판 루프가 끝난 직후 실행)
        # ==========================================
        if plate['storage'] == "CSV" and new_metadata_records:
            df_new = pd.DataFrame(new_metadata_records)
            if os.path.exists(CSV_PATH):
                df_existing = pd.read_csv(CSV_PATH)
                df_combined = pd.concat([df_existing, df_new], ignore_index=True)
                df_final = df_combined.drop_duplicates(subset=['filename'], keep='last')
                df_final.to_csv(CSV_PATH, index=False, encoding="utf-8-sig")
                print(f"📊 [CSV 저장] {plate['name']} 메타데이터 {len(df_final)}건 갱신 완료!")
            else:
                df_new.to_csv(CSV_PATH, index=False, encoding="utf-8-sig")
                print(f"📊 [CSV 생성] {plate['name']} 메타데이터 {len(df_new)}건 저장 완료!")
                
        print(f"✅ [{plate['name']}] 처리가 모두 완료되었습니다. (신규: {total_loaded_posts}건)\n")
        time.sleep(random.uniform(2, 4))

# ==========================================
# 5. 파이프라인 실행 트리거
# ==========================================
if __name__ == "__main__":
    run_v4_pipeline()
    print("🏁 전체 4개 탭 수집 파이프라인이 정상적으로 완료되었습니다!")