import traceback
import os
import json
import random
import concurrent.futures
from botocore.config import Config
import fitz
from flask import (
    Flask, render_template, request, send_from_directory,
    jsonify, stream_with_context, Response, abort
)
import boto3
from boto3 import client
from boto3.dynamodb.conditions import Key
import logging
from botocore.exceptions import ClientError # 예외 처리용

app = Flask(__name__)
config = Config(read_timeout=120, connect_timeout=10)

bedrock = client(
    service_name="bedrock-runtime",
    region_name="us-east-1",
    config=config
)

# --- AWS 설정 ---
s3_client = boto3.client('s3')
dynamodb_resource = boto3.resource('dynamodb')
BOOK_META_DATA_TABLE_NAME = 'BookMetaDataTable'
book_meta_data_table = dynamodb_resource.Table(BOOK_META_DATA_TABLE_NAME)
SOURCE_BUCKET_NAME = os.getenv('arn:aws:s3:::biz-bracelet-book-game-raw-books', 'biz-bracelet-book-game-raw-books')
BOOK_COVERS_FOLDER_PREFIX = 'book-covers/'

BOOK_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'books')
FILE_TITLES = json.load(open("books/titles.json", encoding="utf-8"))
session_histories = {}

# 로그 생성
logger = logging.getLogger()

# 로그의 출력 기준 설정
logger.setLevel(logging.INFO)

@app.route("/files")
def list_files():
    try:
        response = book_meta_data_table.query(
            IndexName='status-index',  # GSI 이름 지정
            KeyConditionExpression=Key('status').eq('PROCESSED')
            )
        logging.info("DynamoDB response:", response)
        items = response['Items']

        # 각 항목에 대한 이미지 URL 생성
        for item in items:
            book_id = item.get('bookId') 

            if book_id:
                # 이미지 파일명 규칙: bookId.jpg (예: golden_goose.jpg)
                # S3 키는 폴더 접두사와 파일명을 결합합니다.
                image_key = f"{BOOK_COVERS_FOLDER_PREFIX}{book_id}.jpg"
                item['cover_image_url'] = get_s3_image_url(SOURCE_BUCKET_NAME, image_key)
            else:
                # bookId가 없는 경우 대체 이미지 URL
                logging.error("❌ S3 URL 생성 오류")
                item['cover_image_url'] = get_s3_image_url(SOURCE_BUCKET_NAME, f"{BOOK_COVERS_FOLDER_PREFIX}default_cover.jpg")

    except Exception:
        items = []
        logging.error("DynamoDB query failed", exc_info=True)
    return render_template("files.html", items=items)

SUMMARY_FOLDER = os.path.join(os.path.dirname(__file__), 'summary')

@app.route("/summary/<filename>")
def get_summary(filename):
    summary_path = os.path.join(SUMMARY_FOLDER, filename)
    if not os.path.isfile(summary_path):
        return jsonify({"error": "요약 파일이 없습니다."}), 404
    try:
        with open(summary_path, encoding="utf-8") as f:
            full_summary = f.read()
        return jsonify({"full_summary": full_summary})
    except Exception as e:
        return jsonify({"error": "서버 파일 읽기 오류"}), 500
    
@app.route('/resource/<path:filename>')
def resource_static(filename):
    return send_from_directory('resource', filename)

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/profile")
def profile():
    bookId = request.args.get('bookId')

    if not bookId:
        return render_template("profile.html", character_summary=None, character_info=None, title=None, full_summary=None, bookId=bookId)

    try:
        future = book_meta_data_table.get_item(Key={'bookId': bookId})
        
        character_summary_for_view = future.get("Item", {}).get('protagonist_for_view', {})
        character_summary = future.get("Item", {}).get('protagonist', {})
        character_info = character_summary.get('summary', '')
        title = future.get("Item", {}).get('title', 'Unknown Title')
        full_summary = convert_book_metadata_to_structured_text(future.get("Item", {}))

    except Exception:
        print("❌ Error in /profile:", traceback.format_exc())
        character_summary = None
        character_info = None

    return render_template("profile.html", character_summary=character_summary_for_view, character_info=character_info, title=title, full_summary=full_summary, bookId=bookId)    

@app.route("/profileByNewStory")
def profileByNewStory():
    return render_template("profileByNewStory.html")

@app.route("/game")
def game():
    return render_template("game.html")

@app.route("/chat/stream", methods=["POST"])
def chat_stream():
    data = request.get_json()
    user_msg = data.get("message")
    summary = data.get("summary")
    session_id = data.get("sessionId")

    print("session_id:", session_id)
    history = session_histories.get(session_id, [])
    def stream_response():
        try:
            if summary and not history:
                yield from initialize_session_context(summary, session_id)
            else:
                yield from stream_agent_response(user_msg, session_id)
        except Exception:
            yield "[오류] 서버 처리 중 문제가 발생했습니다."
            traceback.print_exc()

    return Response(stream_with_context(stream_response()), content_type='text/plain')

def get_s3_image_url(bucket_name, image_key):
    """
    S3 객체에 대한 미리 서명된(pre-signed) URL을 생성합니다.
    """
    try:
        s3_client.head_object(Bucket=bucket_name, Key=image_key)
        response = s3_client.generate_presigned_url(
            'get_object',
            Params={'Bucket': bucket_name, 'Key': image_key},
            ExpiresIn=604800 # <-- 유효시간 7일(604800초)로 설정
        )
    except ClientError as e:
        logger.error(f"❌ S3 URL 생성 오류: {e}")
        response = s3_client.generate_presigned_url(
            'get_object',
            Params={'Bucket': bucket_name, 'Key': f"{BOOK_COVERS_FOLDER_PREFIX}default_cover.jpg"},
            ExpiresIn=604800 # <-- 유효시간 7일(604800초)로 설정
        )

    return response

def read_file_content(filepath):
    ext = os.path.splitext(filepath)[1].lower()
    
    if ext == ".pdf":
        # PDF 파일인 경우: PyMuPDF 사용
        text = ""
        try:
            doc = fitz.open(filepath)
            for page in doc:
                text += page.get_text()
            doc.close()
            return text
        except Exception as e:
            print(f"❌ PDF 읽기 오류: {e}")
            return "[PDF 읽기 실패]"
    else:
        # 텍스트 파일 등 일반 파일: UTF-8로 처리
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            print(f"❌ 텍스트 파일 읽기 오류: {e}")
            return "[텍스트 읽기 실패]"

def get_file_content(file):
    ext = os.path.splitext(file.filename)[1].lower()
    raw = file.read()

    if ext == ".pdf":
        try:
            doc = fitz.open(stream=raw, filetype="pdf")
            text = ""
            for page in doc:
                text += page.get_text()
            doc.close()
            return text
        except Exception as e:
            print(f"❌ PDF 읽기 오류: {e}")
            return "[PDF 읽기 실패]"

    try:
        return raw.decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"❌ 텍스트 디코딩 오류: {e}")
        return "[텍스트 읽기 실패]"
        
def initialize_session_context(summary, session_id):
    system_prompt = f"""
You are creating a text adventure game based on the provided summary.  
Set the game's background world using the summary.  
The protagonist is the same as described in the Summary.
Modify the character sheet through the game based on the player's choise but do not show the character sheet to the player.
Develop major episodes based on scenes from the summary to use throughout the gameplay.

Once at the beginning of the conversation, Present a prologue that explains the basic background and world overview.
Addressing the player as "You" in the second-person perspective.
After the prologue, show the first episode of the plot from the summary and ask the player what they want to do next.
Prologue and the first episode should not be presented except when the game starts.
Episode title and Prologue title, Epilogue title should be in <h1> tag.

Provide numbered examples of appropriate actions to guide the player. Each examples should be in div class "example" tag.
And in the examples, if the original storyline exist, mark it at the end of the example with "<추천>" in font color yellow.
Examples should be placed under the line of instruction, saying the player and choose or type the action.

The instruction should be in following format:
<br><p style="color: yellow; font-style: italic;"><instruction></p>

When a character speaks, use the following format:
<I>"<character_speech>"</I>
The character_speech should be in font type bold and italic.

When the player responds, continue the story based on the world you have created.  
The session continues until the story reaches its conclusion.  
Avoid repeating the same episodes to keep the experience engaging.  
If there are not enough episodes, create new ones consistent with the world and the player’s journey.
Avoid violent or explicit content.

IMPORTANT:  
When the place where the story stays changed, insert the special token [NEW_PLACE] in the response.  
This token will be used by the system to detect place changes and trigger image generation.
Don't use special letters other than listed follow : ',.-:?!~"()[]<>

If the user needs to correct information, they will do so in the format [[INFORMATION]]. 
Never mention the format [[INFORMATION]] to the user and do not change the setting without the format.
When the plot changed out of the summary's worldbuilding, make the game end with a warning.

The entire session should be conducted in Korean unless otherwise instructed.

Summary:
\"\"\"{summary}\"\"\"
"""

    history = session_histories.get(session_id, [])
    history.append({"role": "user", "content": [{"text": system_prompt}, {"text": "게임 시작"}]})

    system_prompts = [{"text": system_prompt}]
    
    response = bedrock.converse_stream(
        modelId="us.anthropic.claude-sonnet-4-20250514-v1:0",
        system=system_prompts,
        messages=history,
        inferenceConfig={"temperature": 0.7},
        additionalModelRequestFields={"top_k": 250}
    )

    print(history)
    assistant_reply = ""
    stream = response.get('stream')
    if stream:
        for event in stream:
            if 'contentBlockDelta' in event:
                text = event['contentBlockDelta']['delta'].get('text', '')
                if text:
                    assistant_reply += text.replace("\\n", "\n")
                    yield text.replace("\\n", "\n")

    history.append({"role": "assistant", "content": [{"text": assistant_reply}]})
    session_histories[session_id] = history

def stream_agent_response(user_input, session_id):
    try:
        history = session_histories.get(session_id, [])
        history.append({"role": "user", "content": [{"text": user_input}]})

        response = bedrock.converse_stream(
            modelId="us.anthropic.claude-sonnet-4-20250514-v1:0",
            messages=history,
            inferenceConfig={"temperature": 0.7},
            additionalModelRequestFields={"top_k": 250}
        )

        assistant_reply = ""
        stream = response.get('stream')
        if stream:
            for event in stream:
                if 'contentBlockDelta' in event:
                    text = event['contentBlockDelta']['delta'].get('text', '')
                    if text:
                        assistant_reply += text.replace("\\n", "\n")
                        yield text.replace("\\n", "\n")

        history.append({"role": "assistant", "content": [{"text": assistant_reply}]})
        session_histories[session_id] = history

    except Exception:
        traceback.print_exc()
        yield "[오류] 응답 처리 중 문제가 발생했습니다. 잠시 후 다시 시도해주세요."

def invoke_full_summary(content):
    prompt = f"""
Based on the following text, please provide detailed and vivid explanations for each of the following aspects. 
For the protagonist, describe in rich detail including name, estimated age, gender, clothing style, hair color and style, eye color, and any other distinctive physical features. 
Also include personality, background, and motivations. 
The goal is to provide enough visual and narrative detail to create an AI-generated text game and it's storyline.

Please express the worldbuilding and the temporal/spatial settings as vividly and specifically as possible.
For the key events, provide a detailed summary of the most significant events in the story, including their impact on the protagonist and the world.
key events will be used in building the main storyline of the game.

The total maximum token limit is 3000, so be concise but thorough.

1. Protagonist (include name, estimated age, gender, appearance, personality, background, and motivation)
2. Worldbuilding (environment, atmosphere, rules, tone, etc.)
3. Temporal/Spatial Setting (historical context, geography, culture, architecture, etc.)
4. Plot Summary (important developments and narrative flow)
5. Key Events (with detailed descriptions)

Text:
\"\"\"{content}\"\"\"
"""

    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 3000,
        "temperature": 0.7,
        "messages": [{"role": "user", "content": prompt}]
    }

    response = bedrock.invoke_model(
        modelId="us.anthropic.claude-sonnet-4-20250514-v1:0",
        body=json.dumps(body),
        contentType="application/json",
        accept="application/json",
    )
    result = json.loads(response['body'].read())
    if "content" in result and isinstance(result["content"], list):
        return "".join(part.get("text", "") for part in result["content"])
    return ""


def invoke_character_summary(content):
    prompt = f"""
Analyze the following story and extract a concise visual description of the main protagonist.

Output it as **a single line**, using comma-separated phrases only — no full sentences or bullet points.

Format:
[Estimated Age], [Gender], [Skin Tone], [Eye Color], [Hair Color], [General Clothing Style and Era or Region], [Optional: Personality expression or visual vibe]

Guidelines:
- Avoid sensitive regional, racial, or political labels. Use descriptive, neutral terms (e.g., "desert region" instead of "Middle Eastern", "19th-century rural attire" instead of "American working-class").
- If any detail is missing, infer plausibly from context. Do not leave blanks.
- Ensure the description is compact and under 300 characters.
- Do not mention the word “style”, “tone”, or “era” explicitly — describe visually instead.

Example:
Late 20s, Male, Sun-kissed skin, Hazel eyes, Black wavy hair, Simple tunic and scarf from a dry, rural land, Calm but alert expression

Now analyze the story and output just **one line** in the above format:

Story:
\"\"\"{content}\"\"\"
"""

    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 300,
        "temperature": 0.7,
        "messages": [{"role": "user", "content": prompt}]
    }

    response = bedrock.invoke_model(
        modelId="us.anthropic.claude-3-5-haiku-20241022-v1:0",
        body=json.dumps(body),
        contentType="application/json",
        accept="application/json",
    )
    result = json.loads(response['body'].read())

    print("invoke_character_summary:", result)
    if "content" in result and isinstance(result["content"], list):
        return "".join(part.get("text", "") for part in result["content"])
    return ""


def get_character_info_json(content):
    prompt = f"""
아래 이야기에서 주인공의 이름, 나이, 성별, 성격, 배경을 추정해서 다음 JSON 형식으로 응답하세요. 문장은 넣지 말고 오직 JSON만 출력하세요. 
Value는 다른 지시사항이 없는 한 한글로 적고, 한글로 적을 수 없으면 영어로 적은 후 한글로 번역해줘.
appearance는 아래 양식에 맞게 영어로 적어줘.
[Estimated Age], [Gender], [Skin Tone], [Eye Color], [Hair Color], [General Clothing Style and Era or Region], [Optional: Personality expression or visual vibe]
예시:
Late 20s, Male, Sun-kissed skin, Hazel eyes, Black wavy hair, Simple tunic and scarf from a dry, rural land, Calm but alert expression


{{
  "name": "캐릭터 이름 또는 별명",
  "age": "추정 나이",
  "gender": "성별",
  "personality": "성격 요약",
  "background": "배경 요약",
  "appearance": "외형"
}}

다른 설명 없이 JSON만 출력해주세요.

이야기:
\"\"\"{content}\"\"\"
"""

    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 400,
        "temperature": 0.7,
        "messages": [{"role": "user", "content": prompt}]
    }

    response = bedrock.invoke_model(
        modelId="us.anthropic.claude-3-sonnet-20240229-v1:0",
        body=json.dumps(body),
        contentType="application/json",
        accept="application/json",
    )

    raw = response['body'].read()
    print("get_character_info_json:", raw)
    text = raw.decode() if isinstance(raw, bytes) else raw

    try:
        outer = json.loads(text)
        if isinstance(outer, dict) and "content" in outer and len(outer["content"]) > 0:
            inner_text = outer["content"][0].get("text", "")
            return json.loads(inner_text)
        else:
            return {"error": "content 내부에 텍스트가 없습니다.", "raw": outer}
    except Exception:
        return {"error": "AI 응답을 JSON으로 파싱하지 못했습니다.", "raw_response": text}


def create_character_image(character_summary):
    prompt = f"""
draw one face in one of following styles: 
'realistic game illustration', 'japanese anime', 'webtoon', 'classic art', 'realistic photo'.
The image must show only one head. never show full body.
avoid any violent or sensitive terms.
\"\"\"{character_summary}\"\"\"
"""

    body = {
        "taskType": "TEXT_IMAGE",
        "textToImageParams": {
            "text": prompt
        },
        "imageGenerationConfig": {
            "seed": random.randint(1, 858993460),
            "quality": "standard",
            "height": 320,
            "width": 320,
            "numberOfImages": 3,
        }
    }

    response = bedrock.invoke_model(
        modelId="amazon.nova-canvas-v1:0",
        body=json.dumps(body),
        contentType="application/json",
        accept="application/json",
    )
    result = json.loads(response['body'].read())
    return result.get("images", [])

def convert_book_metadata_to_structured_text(book_details_dict):
    structured_text_parts = []

    # 1. Protagonist
    structured_text_parts.append("## 1. Protagonist\n")
    protagonist = book_details_dict.get("protagonist", {})
    if protagonist:
        structured_text_parts.append(f"**Name:** {protagonist.get('name', '')}\n")
        structured_text_parts.append(f"**Age:** {protagonist.get('age', '')}\n")
        structured_text_parts.append(f"**Gender:** {protagonist.get('gender', '')}\n")
        structured_text_parts.append(f"**Appearance:** {protagonist.get('appearance', '')}\n")
        structured_text_parts.append(f"**Personality:** {protagonist.get('personality', '')}\n")
        structured_text_parts.append(f"**Background:** {protagonist.get('background', '')}\n")
        structured_text_parts.append(f"**Summary (Concise):** {protagonist.get('summary', '')}\n")
    else:
        structured_text_parts.append("No protagonist details found.\n") # 값이 아예 없을 때 명시
    structured_text_parts.append("\n")

    # 2. Worldbuilding
    structured_text_parts.append("## 2. Worldbuilding\n")
    worldbuilding_text = book_details_dict.get("worldbuilding", '')
    structured_text_parts.append(f"{worldbuilding_text}\n")
    structured_text_parts.append("\n")

    # 3. Temporal/Spatial Setting
    structured_text_parts.append("## 3. Temporal/Spatial Setting\n")
    temporal_spatial_setting_text = book_details_dict.get("temporalSpatialSetting", '')
    structured_text_parts.append(f"{temporal_spatial_setting_text}\n")
    structured_text_parts.append("\n")

    # 4. Plot Summary
    structured_text_parts.append("## 4. Plot Summary\n")
    plot_summary_text = book_details_dict.get("plotSummary", '')
    structured_text_parts.append(f"{plot_summary_text}\n")
    structured_text_parts.append("\n")

    # 5. Key Events
    structured_text_parts.append("## 5. Key Events\n")
    key_events = book_details_dict.get("keyEvents", [])
    if key_events:
        for event in key_events:
            episode_num = event.get('episode_num', '')
            event_summary = event.get('event_summary', '')
            # 에피소드 번호가 없으면 "Episode : " 대신 공백 처리
            episode_prefix = f"**Episode {episode_num}:** " if episode_num else ""
            structured_text_parts.append(f"{episode_prefix}{event_summary}\n")
    else:
        structured_text_parts.append("No key events found.\n")
    structured_text_parts.append("\n")

    # 6. Ending Summary
    structured_text_parts.append("## 6. Ending Summary\n")
    ending_summary_text = book_details_dict.get("endingSummary", '')
    structured_text_parts.append(f"{ending_summary_text}\n")
    structured_text_parts.append("\n")

    return "".join(structured_text_parts).strip()

@app.route("/profile_data", methods=["POST"])
def profile_data():
    data = request.get_json()
    summary = data.get("summary")
    if not summary:
        return jsonify({"error": "summary 제공되지 않았습니다."}), 400

    try:
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future_char = executor.submit(invoke_character_summary, summary)
            future_info = executor.submit(get_character_info_json, summary)

            character_summary = future_char.result()
            character_info = future_info.result()

        return jsonify({
            "character_summary": character_summary,
            "character_info": character_info
        })

    except Exception:
        print("❌ Error in /profile_data:", traceback.format_exc())
        return jsonify({"error": "프로필 데이터를 불러오는 중 오류 발생."}), 500

@app.route("/summary", methods=["POST"])
def summary():
    uploaded = request.files.get('file')
    if not uploaded:
        return jsonify({"error": "파일이 업로드되지 않았습니다."}), 400

    try:
        content = get_file_content(uploaded)

        full_summary = invoke_full_summary(content)
        print("full_summary:", full_summary)
        return jsonify({"full_summary": full_summary})

    except Exception:
        print("❌ Error in /summary:", traceback.format_exc())
        return jsonify({"error": "요약 중 오류 발생."}), 500

@app.route("/profile_image_generate", methods=["POST"])
def profile_image_generate():
    try:
        data = request.get_json()
        character_info = data.get("character_info")
        if not character_info:
            return jsonify({"error": "character_info 제공되지 않았습니다."}), 400

        images = create_character_image(character_info)
        return jsonify({"images": images})
    except Exception:
        print("❌ Error in /profile_image_generate:", traceback.format_exc())
        return jsonify({"error": "이미지 생성 중 오류 발생."}), 500

@app.route("/background_generate", methods=["POST"])
def background_generate():
    try:
        data = request.get_json()
        place = data.get("place")
        if not place:
            return jsonify({"error": "place가 제공되지 않았습니다."}), 400

        # Claude 3.5 Haiku 프롬프트 구성
        haiku_prompt = f"""Please describe the background setting based on the following input, suitable for an AI image generation model. Be descriptive and focus on environment and mood without including people or violent content.

\"\"\"{place}\"\"\"
"""

        haiku_body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 300,
            "temperature": 0.7,
            "messages": [{"role": "user", "content": haiku_prompt}]
        }

        haiku_response = bedrock.invoke_model(
            modelId="us.anthropic.claude-3-5-haiku-20241022-v1:0",
            body=json.dumps(haiku_body),
            contentType="application/json",
            accept="application/json"
        )
        haiku_result = json.loads(haiku_response['body'].read())
        print("Claude 3.5 Haiku result:", json.dumps(haiku_result, indent=2))

        if "content" in haiku_result and isinstance(haiku_result["content"], list):
            image_prompt = "".join(part.get("text", "") for part in haiku_result["content"])
        else:
            return jsonify({"error": "Claude 응답이 올바르지 않습니다."}), 500

        # Nova Canvas 이미지 생성 요청
        canvas_body = {
            "taskType": "TEXT_IMAGE",
            "textToImageParams": {
                "text": image_prompt[:1023]
            },
            "imageGenerationConfig": {
                "seed": random.randint(1, 858993460),
                "quality": "standard",
                "height": 320,
                "width": 960,
                "numberOfImages": 1,
            }
        }

        response = bedrock.invoke_model(
            modelId="amazon.nova-canvas-v1:0",
            body=json.dumps(canvas_body),
            contentType="application/json",
            accept="application/json"
        )

        result = json.loads(response['body'].read())
        return jsonify({"images": result.get("images", [])})

    except Exception:
        print("❌ Error in /background_generate:", traceback.format_exc())
        return jsonify({"error": "이미지 생성 중 오류 발생."}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=True)
