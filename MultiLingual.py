from fastapi import FastAPI, Request, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from gtts import gTTS
import os
from uuid import uuid4
import langid
from deep_translator import GoogleTranslator
import whisper
from langchain_community.document_loaders import PyMuPDFLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.vectorstores import Chroma, FAISS
from langchain_community.embeddings import OpenAIEmbeddings

from langchain_community.chat_models import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
import shutil
# test
app = FastAPI()

os.environ["OPENAI_API_KEY"] = ''

persist_directory = "./storage"
uploaded_pdfs_directory = "uploaded_pdfs"
os.makedirs(uploaded_pdfs_directory, exist_ok=True)

templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

pdf_vectorstores = {}
current_pdf_id = None

def initialize_vectordb(pdf_path: str, pdf_id: str):
    global pdf_vectorstores, current_pdf_id

    storage_path = f"{persist_directory}/{pdf_id}"
    if os.path.exists(storage_path):
        shutil.rmtree(storage_path)

    loader = PyMuPDFLoader(pdf_path)
    documents = loader.load()

    text_splitter = RecursiveCharacterTextSplitter(chunk_size=512, chunk_overlap=10)
    texts = text_splitter.split_documents(documents)

    embeddings = OpenAIEmbeddings()

    try:
        vectordb = Chroma.from_documents(documents=texts, 
                                         embedding=embeddings,
                                         persist_directory=storage_path)
    except Exception as e:
        print(f"Error with Chroma: {e}")
        print("Switching to FAISS...")
        vectordb = FAISS.from_documents(documents=texts, embedding=embeddings)

    vectordb.persist()
    pdf_vectorstores[pdf_id] = vectordb.as_retriever(search_kwargs={"k": 3})
    current_pdf_id = pdf_id

@app.post("/upload-pdf")
async def upload_pdf(file: UploadFile = File(...)):
    try:
        pdf_id = str(uuid4())
        pdf_path = f"{uploaded_pdfs_directory}/{pdf_id}_{file.filename}"
        with open(pdf_path, "wb") as f:
            f.write(await file.read())

        initialize_vectordb(pdf_path, pdf_id)
        return {"message": "PDF uploaded and processed successfully."}
    except Exception as e:
        print(f"Error during PDF upload: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to upload PDF: {e}")

@app.get("/")
def index(request: Request):
    return templates.TemplateResponse("index4.html", {"request": request})

@app.post("/ask")
async def ask(request: Request):
    try:
        data = await request.json()
        user_input = data['query']
        print(user_input)
       
        if data.get('audio'):
            audio_file = data['audio']
            model = whisper.load_model("base")
            result = model.transcribe(audio_file)
            user_input = result['text']
        
        input_lang, _ = langid.classify(user_input)

        if input_lang != 'en':
            user_input = GoogleTranslator(source=input_lang, target='en').translate(user_input)

        if not current_pdf_id or current_pdf_id not in pdf_vectorstores:
            return JSONResponse(content={"error": "No PDF has been uploaded yet."}, status_code=400)

        retriever = pdf_vectorstores[current_pdf_id]
        resp_obj = retriever.get_relevant_documents(user_input)

        PROMPT_TEMPLATE = """
        Answer the question based only on the following context:

        {context}

        ---

        Answer the question based on the above context: {question}
        """

        context_text = '\n\n---\n\n'.join([resp.page_content for resp in resp_obj])

        prompt_template = ChatPromptTemplate.from_template(PROMPT_TEMPLATE)
        prompt = prompt_template.format(context=context_text, question=user_input)
        model = ChatOpenAI(model='gpt-4-turbo')
        response_text = model.predict(prompt)
        print(response_text)


        if input_lang != 'en':
            response_text = GoogleTranslator(source='en', target=input_lang).translate(response_text)

        tts = gTTS(response_text, lang=input_lang)
        audio_file = f"static/{uuid4()}.mp3"
        tts.save(audio_file)

        return JSONResponse(content={"response": response_text, "audio_url": f"/{audio_file}"})
    except Exception as e:
        print(f"Error during query processing: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
