import os
import hashlib
import aiofiles
import aiofiles.os
import sys
from typing import Iterable, List, Optional
from shutil import copyfileobj

from pymongo import MongoClient
from payos import PaymentData, ItemData, PayOS

from datetime import datetime
from dateutil.relativedelta import relativedelta
import json

import uvicorn
from langchain.schema import Document
from contextlib import asynccontextmanager
from dotenv import find_dotenv, load_dotenv
from fastapi.middleware.cors import CORSMiddleware
from langchain_core.runnables.config import run_in_executor
from langchain.text_splitter import RecursiveCharacterTextSplitter
from fastapi import (
    File,
    Form,
    Body,
    Query,
    status,
    FastAPI,
    Request,
    UploadFile,
    HTTPException,
)
from langchain_community.document_loaders import (
    WebBaseLoader,
    TextLoader,
    PyPDFLoader,
    CSVLoader,
    Docx2txtLoader,
    UnstructuredEPubLoader,
    UnstructuredMarkdownLoader,
    UnstructuredXMLLoader,
    UnstructuredRSTLoader,
    UnstructuredExcelLoader,
    UnstructuredPowerPointLoader,
)

from models import (
    StoreDocument,
    QueryRequestBody,
    DocumentResponse,
    QueryMultipleBody,
)
from psql import PSQLDatabase, ensure_custom_id_index_on_embedding, pg_health_check
from pgvector_routes import router as pgvector_router
from parsers import process_documents, clean_text
from middleware import security_middleware
from mongo import mongo_health_check
from constants import ERROR_MESSAGES
from store import AsyncPgVector

load_dotenv(find_dotenv())

from config import (
    logger,
    debug_mode,
    CHUNK_SIZE,
    CHUNK_OVERLAP,
    vector_store,
    RAG_UPLOAD_DIR,
    known_source_ext,
    PDF_EXTRACT_IMAGES,
    LogMiddleware,
    RAG_HOST,
    RAG_PORT,
    VectorDBType,
    # RAG_EMBEDDING_MODEL,
    # RAG_EMBEDDING_MODEL_DEVICE_TYPE,
    # RAG_TEMPLATE,
    VECTOR_DB_TYPE,
)

# MONGODB
client = MongoClient(os.getenv("ATLAS_MONGO_DB_URI"))
db = client['test']

transactions = db['transactions']
users = db['users']
balances = db['balances']
payments = db['payments']

monthlyTokenCreditsArr = [0, 1500000, 3500000, 5000000]

#PAYOS
payOS = PayOS(client_id=os.getenv("PAYOS_CLIENT_ID"), 
              api_key=os.getenv("PAYOS_API_KEY"), 
              checksum_key=os.getenv("PAYOS_CHECKSUM_KEY")) 

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup logic goes here
    if VECTOR_DB_TYPE == "pgvector":
        await PSQLDatabase.get_pool()  # Initialize the pool
        await ensure_custom_id_index_on_embedding()

    yield


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(LogMiddleware)

app.middleware("http")(security_middleware)

app.state.CHUNK_SIZE = CHUNK_SIZE
app.state.CHUNK_OVERLAP = CHUNK_OVERLAP
app.state.PDF_EXTRACT_IMAGES = PDF_EXTRACT_IMAGES


@app.get("/ids")
async def get_all_ids():
    try:
        if isinstance(vector_store, AsyncPgVector):
            ids = await vector_store.get_all_ids()
        else:
            ids = vector_store.get_all_ids()

        return list(set(ids))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def isHealthOK():
    if VECTOR_DB_TYPE == VectorDBType.PGVECTOR:
        return pg_health_check()
    if VECTOR_DB_TYPE == VectorDBType.ATLAS_MONGO:
        return mongo_health_check()
    else:
        return True
    
@app.post("/add-balance")
async def add_balance(email, amount: int):
    amount = int(amount)

    # Validate the email
    if email and '@' not in email:
        return {"Error": "Invalid email address!"}
    
    if amount == 0:
        return {f"Error": "Invalid amount {amount}!"}

    # Validate the user
    user = users.find_one({"email": email})
    if not user:
        return {"Error": "No user with that email was found!"}

    # Create transaction and update balance
    try:
        result = transactions.insert_one({
            "user": user["_id"],
            "tokenType": "credits",
            "context": "admin",
            "rawAmount": amount,
            "tokenValue": amount,
            "rate": 1,
            "ggRate": 1,
            "ggTokenValue": amount * 0.075,
            "createAt": datetime.now(),
            "updateAt": datetime.now()
        })
    except Exception as e:
        return {"Error": str(e)}

    # Check the result
    if not result:
        return {"Error": "Something went wrong while updating the balance!"}
    
    balance = balances.find_one_and_update(
        {"user": user["_id"]},
        {"$inc": {"tokenCredits": amount, "ggTokenCredits": amount * 0.075}
                  ,'$set': { "__v" : 0}},
        upsert=True,
        new=True
    )

    return {
        "message": "Transaction created successfully!",
        "amount": amount,
        "new_balance": balance["tokenCredits"]
    }

@app.get("/payment-history")
async def get_payment_history(userId: Optional[str] = None, email: Optional[str] = None):
    if userId is None and email is None:
        return {
            "status": 400,
            "data": {"message": "userId and email not found"}
        }
    
    # Validate the email format
    if email and '@' not in email:
        return {
            "status": 400,
            "data": {"message": "Invalid email address!"}
        }
    user = None
    if userId is not None:
        # Validate the user by userId
        user = users.find_one({"user": userId})
        if not user:
            return {
                "status": 400,
                "data": {"message": "No user with that userId was found!"}
            }
        elif email is not None:
            # Validate the user by email as well
            user = users.find_one({"email": email})
            if not user:
                return {
                    "status": 400,
                    "data": {"message": "No user with that email was found!"}
                }
    elif email is not None:
        # If only email is provided, validate the user by email
        user = users.find_one({"email": email})
        if not user:
            return {
                "status": 400,
                "data": {"message": "No user with that email was found!"}
            }
            
    payment_history = list(payments.find({"user": user["_id"]}).sort("createAt", -1).limit(20))
    
    try:
        payment_info = payOS.getPaymentLinkInfomation(int(payment_history[0]["orderCode"]))
        payment_info = payment_info.to_json()
        status = "pending"
        if(payment_info["status"] == "CANCELLED"):
            status = "cancelled"
        elif (payment_info["status"] == "PAID"):
            status = "success"
        update_fields = {
            "status": status,
        }
        result = payments.update_one(
            {"orderCode": payment_history[0]["orderCode"]},
            {"$set": update_fields},
        )
        payment_history[0]["status"] = status
    except Exception as e:
        return {"status": 400,
                "data": {"message": str(e)}
            }

    return {"status": 200,
            "data": json.dumps(payment_history, default=str)}

@app.get("/payment-info")
async def get_payment_info(orderCode: int):
    response = payOS.getPaymentLinkInfomation(orderId=orderCode)
    return {
        "status": 200,
        "data": response
    }

@app.post("/subscribe")
async def subscribe(
    userId: Optional[str] = None, 
    email: Optional[str] = None,
    orderCode: int = None,
    context: str = None,
    affectNow: bool = False
):
    # Plan:
    # 0 - Community
    # 1 - Standard
    # 2 - Advanced
    # 3 - Ultimate

    payment = payments.find_one({"orderCode": orderCode})
    if orderCode == None or payment == None:
        return {
                "status": 400,
                "data": {"message": f"Can't find any record match with {orderCode}!"}
            }
    
    plan:int = payment["plan"]
    duration: int = payment["duration"] 
    amount: int = payment["amount"]
    monthlyTokenCredits: int = monthlyTokenCreditsArr[plan]

    if userId is None and email is None:
        return {
            "status": 400,
            "data": {"message": "userId and email not found"}
        }
    
    # Validate the email format
    if email and '@' not in email:
        return {
            "status": 400,
            "data": {"message": "Invalid email address!"}
        }
    
    if amount < 0:
        return {
            "status": 400,
            "data": {"message": f"Invalid amount {amount}!"}
        }
    
    if duration < 0:
        return {
            "status": 400,
            "data": {"message": f"Invalid duration {duration}!"}
        }
    
    if monthlyTokenCredits < 0:
        return {
            "status": 400,
            "data": {"message": f"Invalid monthlyTokenCredits {monthlyTokenCredits}!"}
        }
    
    context_arr = ["subscribe" , "renew" , "upgrade" , "downgrade"]
    if context not in context_arr:
        return {
            "status": 400,
            "data": {"message": f"Invalid context {context}!"}
        }
    
    user = None
    if userId is not None:
        # Validate the user by userId
        user = users.find_one({"user": userId})
        if not user:
            return {
                "status": 400,
                "data": {"message": "No user with that userId was found!"}
            }
        elif email is not None:
            # Validate the user by email as well
            user = users.find_one({"email": email})
            if not user:
                return {
                    "status": 400,
                    "data": {"message": "No user with that email was found!"}
                }
    elif email is not None:
        # If only email is provided, validate the user by email
        user = users.find_one({"email": email})
        if not user:
            return {
                "status": 400,
                "data": {"message": "No user with that email was found!"}
            }
            

    if(payment["user"] != user["_id"]):
        return {
                "status": 400,
                "data": {"message": "No user with that orderCode was found!"}
            }

    if(affectNow == False):
       return {
                "status": 200,
                "data": {"message": "Transaction update later!"}
            }
    
    if(payment["status"] == "success"):
        return {
                "status": 200,
                "data": {"message": "Transaction is already updated!"}
            }
    # Check if not using free plan
    if plan != 0:
        # Create transaction and update balance
        try:
            update_fields = {
                "context": context,
                "monthlyTokenCredits": monthlyTokenCredits,
                "remainMonthlyTokenCredits": monthlyTokenCredits,
                "handled": affectNow,
                "status": "success",
                "createAt": datetime.now(),
            }

            result = payments.update_one(
                {"orderCode": orderCode},
                {"$set": update_fields},
            )
            print(result)
        except Exception as e:
            return {"status": 400,
                    "data": {"message": str(e)}
                }
    
    update_fields = {
        "__v": 0,
    }
    inc_fields = {}

    tmp = balances.find_one({"user": user["_id"]})
    if tmp == None:
        remain_monthly_token_credits = 0
    else:    
        remain_monthly_token_credits = tmp.get('remainMonthlyTokenCredits', None)
    
    if context == "upgrade" or context == "downgrade":
        update_fields = {
            "__v": 0,
            "plan": plan,
            "monthlyTokenCredits": monthlyTokenCredits,
            "remainMonthlyTokenCredits": monthlyTokenCredits,
            "expiredAt": datetime.today() + relativedelta(months=duration)
        }
        inc_fields = {
            "tokenCredits": remain_monthly_token_credits
        }
    elif context == "subscribe":
        if plan == 0:
            update_fields = {
                "__v": 0,
                "plan": plan,
                "monthlyTokenCredits": remain_monthly_token_credits,
                "remainMonthlyTokenCredits": remain_monthly_token_credits,
                "expiredAt": datetime.today() + relativedelta(months=duration)
            }
            inc_fields = {
                "tokenCredits": monthlyTokenCredits
            }
        else:
            update_fields = {
                "__v": 0,
                "plan": plan,
                "monthlyTokenCredits": monthlyTokenCredits,
                "remainMonthlyTokenCredits": monthlyTokenCredits,
                "expiredAt": datetime.today() + relativedelta(months=duration)
            }
            inc_fields = {
                "tokenCredits": 0
            }
    else:
        update_fields = {
            "__v": 0,
            "monthlyTokenCredits": monthlyTokenCredits,
            "remainMonthlyTokenCredits": monthlyTokenCredits,
            "expiredAt": datetime.today() + relativedelta(months=duration)
        }
        inc_fields = {
            "tokenCredits": remain_monthly_token_credits
        }

    balance = balances.find_one_and_update(
        {"user": user["_id"]},
        {"$inc": inc_fields,
         "$set": update_fields},
        upsert=True,
        new=True
    )

    return {
        "status": 200,
        "data": {"message": "Transaction created successfully!",
                 "user": user["email"],
                 "amount": amount,
                 "tokenCredits": balance["tokenCredits"],
                 "monthlyTokenCredits": balance["monthlyTokenCredits"]
        }
    }

@app.post("/buy-credits")
async def buy_credits(
    userId: Optional[str] = None, 
    email: Optional[str] = None, 
    amount: int = None, 
    tokenCredits: int = None,
):
    if userId is None and email is None:
        return {
            "status": 400,
            "data": {"message": "userId and email not found"}
        }
    
    # Validate the email format
    if email and '@' not in email:
        return {
            "status": 400,
            "data": {"message": "Invalid email address!"}
        }
    
    if amount < 0:
        return {
            "status": 400,
            "data": {"message": "Invalid amount {amount}!"}
        }
    
    if tokenCredits < 0:
        return {
            "status": 400,
            "data": {"message": "Invalid monthlyTokenCredits {tokenCredits}!"}
        }
    
    user = None
    if userId is not None:
        # Validate the user by userId
        user = users.find_one({"user": userId})
        if not user:
            return {
                "status": 400,
                "data": {"message": "No user with that userId was found!"}
            }
        elif email is not None:
            # Validate the user by email as well
            user = users.find_one({"email": email})
            if not user:
                return {
                    "status": 400,
                    "data": {"message": "No user with that email was found!"}
                }
    elif email is not None:
        # If only email is provided, validate the user by email
        user = users.find_one({"email": email})
        if not user:
            return {
                "status": 400,
                "data": {"message": "No user with that email was found!"}
            }

    inc_fields = {
        "tokenCredits": tokenCredits
    }

    balance = balances.find_one_and_update(
        {"user": user["_id"]},
        {"$inc": inc_fields},
        upsert=True,
        new=True
    )

    return {
        "status": 200,
        "data": {"message": "Transaction created successfully!",
                 "user": user["email"],
                 "amount": amount,
                 "tokenCredits": balance["tokenCredits"],
                 "monthlyTokenCredits": balance["monthlyTokenCredits"]
        }
    }

@app.post("/create-payment-link")
async def create_payment_link(userId: Optional[str] = None, 
                              email: Optional[str] = None,
                              orderCode: int = None, 
                              plan: int = None, 
                              duration: int = None, 
                              amount: int = None,
                              cancelUrl: str = None,
                              returnUrl: str = None):
    if userId is None and email is None:
        return {
            "status": 400,
            "data": {"message": "userId and email not found"}
        }
    
    # Validate the email format
    if email and '@' not in email:
        return {
            "status": 400,
            "data": {"message": "Invalid email address!"}
        }
    
    user = None
    if userId is not None:
        # Validate the user by userId
        user = users.find_one({"user": userId})
        if not user:
            return {
                "status": 400,
                "data": {"message": "No user with that userId was found!"}
            }
        elif email is not None:
            # Validate the user by email as well
            user = users.find_one({"email": email})
            if not user:
                return {
                    "status": 400,
                    "data": {"message": "No user with that email was found!"}
                }
    elif email is not None:
        # If only email is provided, validate the user by email
        user = users.find_one({"email": email})
        if not user:
            return {
                "status": 400,
                "data": {"message": "No user with that email was found!"}
            }

    if plan < 0 or plan > 4:
        return {
                "status": 400,
                "data": {"message": "Plan must be in range [0, 4]!"}
            }
    planName = ["Community", "Standard", "Advanced", "Ultimate", "Buy Credits"]
    item = ItemData(name = planName[plan], quantity=duration, price=amount)
    try:
        if plan < 4:
            paymentData = PaymentData(orderCode=orderCode, 
                                amount=amount, 
                                description= f"{planName[plan]} - {duration}M",
                                items=[item], 
                                cancelUrl= cancelUrl, 
                                returnUrl= returnUrl)
        else:
            paymentData = PaymentData(orderCode=orderCode, 
                                amount=amount, 
                                description= f"{planName[plan]} - {duration}",
                                items=[item], 
                                cancelUrl= cancelUrl, 
                                returnUrl= returnUrl)
        payosCreateResponse = payOS.createPaymentLink(paymentData)
        
        try:
            if plan < 4:
                payments.insert_one({
                    "user": user["_id"],
                    "orderCode": orderCode,
                    "amount": amount,
                    "plan": plan,
                    "duration": duration,
                    "status": "pending",
                    "createAt": datetime.now(),
                })
            else:
                payments.insert_one({
                    "user": user["_id"],
                    "orderCode": orderCode,
                    "amount": amount,
                    "plan": plan,
                    "tokenCredits": duration,
                    "status": "pending",
                    "createAt": datetime.now(),
                })
        except Exception as e:
            return {"status": 400,
                    "data": {"message": str(e)}
                }

        return {"status": 200,
                "data": {json.dumps(payosCreateResponse.to_json())}
            }
    except Exception as e:
        return {"status": 400,
                "data": {"message": str(e)}
            }

@app.post("/receive-webhook")
async def webhook(request:Request):
    webhookBody = await request.json()

    try:
        webhookData = payOS.verifyPaymentWebhookData(webhookBody)
        
        webhookData = webhookData.to_json()

        orderCode = webhookData["orderCode"]

        print(webhookBody["success"])
        status = "cancel"
        if webhookBody["success"] == True:
           status = "success"
        
        payments.find_one_and_update(
            {"orderCode": orderCode},
            {'$set': { "status" : status}},
            upsert=True,
            new=True
        )

        return {
            "status": 200,
            "data": {"message": f"Payment with orderCode: {orderCode} done!"}
        }
    except Exception as e:
        return {   
                "status": 400,
                "data": {"message": str(e)}
               }
    
@app.get("/health")
async def health_check():   
    if await isHealthOK():
        return {"status": "UP"}
    else:
        return {"status": "DOWN"}, 503


@app.get("/documents", response_model=list[DocumentResponse])
async def get_documents_by_ids(ids: list[str] = Query(...)):
    try:
        if isinstance(vector_store, AsyncPgVector):
            existing_ids = await vector_store.get_all_ids()
            documents = await vector_store.get_documents_by_ids(ids)
        else:
            existing_ids = vector_store.get_all_ids()
            documents = vector_store.get_documents_by_ids(ids)

        # Ensure all requested ids exist
        if not all(id in existing_ids for id in ids):
            raise HTTPException(status_code=404, detail="One or more IDs not found")

        # Ensure documents list is not empty
        if not documents:
            raise HTTPException(
                status_code=404, detail="No documents found for the given IDs"
            )

        return documents
    except HTTPException as http_exc:
        raise http_exc
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/documents")
async def delete_documents(document_ids: List[str] = Body(...)):
    try:
        if isinstance(vector_store, AsyncPgVector):
            existing_ids = await vector_store.get_all_ids()
            await vector_store.delete(ids=document_ids)
        else:
            existing_ids = vector_store.get_all_ids()
            vector_store.delete(ids=document_ids)

        if not all(id in existing_ids for id in document_ids):
            raise HTTPException(status_code=404, detail="One or more IDs not found")

        file_count = len(document_ids)
        return {
            "message": f"Documents for {file_count} file{'s' if file_count > 1 else ''} deleted successfully"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/query")
async def query_embeddings_by_file_id(body: QueryRequestBody, request: Request):
    user_authorized = (
        "public" if not hasattr(request.state, "user") else request.state.user.get("id")
    )
    authorized_documents = []

    try:
        embedding = vector_store.embedding_function.embed_query(body.query)

        if isinstance(vector_store, AsyncPgVector):
            documents = await run_in_executor(
                None,
                vector_store.similarity_search_with_score_by_vector,
                embedding,
                k=body.k,
                filter={"file_id": body.file_id},
            )
        else:
            documents = vector_store.similarity_search_with_score_by_vector(
                embedding, k=body.k, filter={"file_id": body.file_id}
            )

        if not documents:
            return authorized_documents

        document, score = documents[0]
        doc_metadata = document.metadata
        doc_user_id = doc_metadata.get("user_id")

        if doc_user_id is None or doc_user_id == user_authorized:
            authorized_documents = documents
        else:
            logger.warn(
                f"Unauthorized access attempt by user {user_authorized} to a document with user_id {doc_user_id}"
            )

        return authorized_documents

    except Exception as e:
        logger.error(e)
        raise HTTPException(status_code=500, detail=str(e))


def generate_digest(page_content: str):
    hash_obj = hashlib.md5(page_content.encode())
    return hash_obj.hexdigest()


async def store_data_in_vector_db(
    data: Iterable[Document],
    file_id: str,
    user_id: str = "",
    clean_content: bool = False,
) -> bool:
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=app.state.CHUNK_SIZE, chunk_overlap=app.state.CHUNK_OVERLAP
    )
    documents = text_splitter.split_documents(data)

    # If `clean_content` is True, clean the page_content of each document (remove null bytes)
    if clean_content:
        for doc in documents:
            doc.page_content = clean_text(doc.page_content)

    # Preparing documents with page content and metadata for insertion.
    docs = [
        Document(
            page_content=doc.page_content,
            metadata={
                "file_id": file_id,
                "user_id": user_id,
                "digest": generate_digest(doc.page_content),
                **(doc.metadata or {}),
            },
        )
        for doc in documents
    ]

    try:
        if isinstance(vector_store, AsyncPgVector):
            ids = await vector_store.aadd_documents(
                docs, ids=[file_id] * len(documents)
            )
        else:
            ids = vector_store.add_documents(docs, ids=[file_id] * len(documents))

        return {"message": "Documents added successfully", "ids": ids}

    except Exception as e:
        logger.error(e)
        return {"message": "An error occurred while adding documents.", "error": str(e)}


def get_loader(filename: str, file_content_type: str, filepath: str):
    file_ext = filename.split(".")[-1].lower()
    known_type = True

    if file_ext == "pdf":
        loader = PyPDFLoader(filepath, extract_images=app.state.PDF_EXTRACT_IMAGES)
    elif file_ext == "csv":
        loader = CSVLoader(filepath)
    elif file_ext == "rst":
        loader = UnstructuredRSTLoader(filepath, mode="elements")
    elif file_ext == "xml":
        loader = UnstructuredXMLLoader(filepath)
    elif file_ext == "pptx":
        loader = UnstructuredPowerPointLoader(filepath)
    elif file_ext == "md":
        loader = UnstructuredMarkdownLoader(filepath)
    elif file_content_type == "application/epub+zip":
        loader = UnstructuredEPubLoader(filepath)
    elif (
        file_content_type
        == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        or file_ext in ["doc", "docx"]
    ):
        loader = Docx2txtLoader(filepath)
    elif file_content_type in [
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ] or file_ext in ["xls", "xlsx"]:
        loader = UnstructuredExcelLoader(filepath)
    elif file_content_type == "application/json" or file_ext == "json":
        loader = TextLoader(filepath, autodetect_encoding=True)
    elif file_ext in known_source_ext or (
        file_content_type and file_content_type.find("text/") >= 0
    ):
        loader = TextLoader(filepath, autodetect_encoding=True)
    else:
        loader = TextLoader(filepath, autodetect_encoding=True)
        known_type = False

    return loader, known_type, file_ext


@app.post("/local/embed")
async def embed_local_file(document: StoreDocument, request: Request):

    # Check if the file exists
    if not os.path.exists(document.filepath):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.FILE_NOT_FOUND,
        )

    if not hasattr(request.state, "user"):
        user_id = "public"
    else:
        user_id = request.state.user.get("id")

    try:
        loader, known_type = get_loader(
            document.filename, document.file_content_type, document.filepath
        )
        data = loader.load()
        result = await store_data_in_vector_db(data, document.file_id, user_id)

        if result:
            return {
                "status": True,
                "file_id": document.file_id,
                "filename": document.filename,
                "known_type": known_type,
            }
        else:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=ERROR_MESSAGES.DEFAULT(),
            )
    except Exception as e:
        logger.error(e)
        if "No pandoc was found" in str(e):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ERROR_MESSAGES.PANDOC_NOT_INSTALLED,
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ERROR_MESSAGES.DEFAULT(e),
            )


@app.post("/embed")
async def embed_file(
    request: Request, file_id: str = Form(...), file: UploadFile = File(...)
):
    response_status = True
    response_message = "File processed successfully."
    known_type = None
    if not hasattr(request.state, "user"):
        user_id = "public"
    else:
        user_id = request.state.user.get("id")

    temp_base_path = os.path.join(RAG_UPLOAD_DIR, user_id)
    os.makedirs(temp_base_path, exist_ok=True)
    temp_file_path = os.path.join(RAG_UPLOAD_DIR, user_id, file.filename)

    try:
        async with aiofiles.open(temp_file_path, "wb") as temp_file:
            chunk_size = 64 * 1024  # 64 KB
            while content := await file.read(chunk_size):
                await temp_file.write(content)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to save the uploaded file. Error: {str(e)}",
        )

    try:
        loader, known_type, file_ext = get_loader(
            file.filename, file.content_type, temp_file_path
        )
        data = loader.load()
        result = await store_data_in_vector_db(
            data=data, file_id=file_id, user_id=user_id, clean_content=file_ext == "pdf"
        )

        if not result:
            response_status = False
            response_message = "Failed to process/store the file data."
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to process/store the file data.",
            )
        elif "error" in result:
            response_status = False
            response_message = "Failed to process/store the file data."
            if isinstance(result["error"], str):
                response_message = result["error"]
            else:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="An unspecified error occurred.",
                )
    except Exception as e:
        response_status = False
        response_message = f"Error during file processing: {str(e)}"
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Error during file processing: {str(e)}",
        )
    finally:
        try:
            await aiofiles.os.remove(temp_file_path)
        except Exception as e:
            logger.info(f"Failed to remove temporary file: {str(e)}")

    return {
        "status": response_status,
        "message": response_message,
        "file_id": file_id,
        "filename": file.filename,
        "known_type": known_type,
    }


@app.get("/documents/{id}/context")
async def load_document_context(id: str):
    ids = [id]
    try:
        if isinstance(vector_store, AsyncPgVector):
            existing_ids = await vector_store.get_all_ids()
            documents = await vector_store.get_documents_by_ids(ids)
        else:
            existing_ids = vector_store.get_all_ids()
            documents = vector_store.get_documents_by_ids(ids)

        # Ensure the requested id exists
        if not all(id in existing_ids for id in ids):
            raise HTTPException(
                status_code=404, detail="The specified file_id was not found"
            )

        # Ensure documents list is not empty
        if not documents:
            raise HTTPException(
                status_code=404, detail="No document found for the given ID"
            )

        return process_documents(documents)
    except Exception as e:
        logger.error(e)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT(e),
        )


@app.post("/embed-upload")
async def embed_file_upload(
    request: Request, file_id: str = Form(...), uploaded_file: UploadFile = File(...)
):
    temp_file_path = os.path.join(RAG_UPLOAD_DIR, uploaded_file.filename)

    if not hasattr(request.state, "user"):
        user_id = "public"
    else:
        user_id = request.state.user.get("id")

    try:
        with open(temp_file_path, "wb") as temp_file:
            copyfileobj(uploaded_file.file, temp_file)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to save the uploaded file. Error: {str(e)}",
        )

    try:
        loader, known_type = get_loader(
            uploaded_file.filename, uploaded_file.content_type, temp_file_path
        )

        data = loader.load()
        result = await store_data_in_vector_db(data, file_id, user_id)

        if not result:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to process/store the file data.",
            )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Error during file processing: {str(e)}",
        )
    finally:
        os.remove(temp_file_path)

    return {
        "status": True,
        "message": "File processed successfully.",
        "file_id": file_id,
        "filename": uploaded_file.filename,
        "known_type": known_type,
    }


@app.post("/query_multiple")
async def query_embeddings_by_file_ids(body: QueryMultipleBody):
    try:
        # Get the embedding of the query text
        embedding = vector_store.embedding_function.embed_query(body.query)

        # Perform similarity search with the query embedding and filter by the file_ids in metadata
        if isinstance(vector_store, AsyncPgVector):
            documents = await run_in_executor(
                None,
                vector_store.similarity_search_with_score_by_vector,
                embedding,
                k=body.k,
                filter={"file_id": {"$in": body.file_ids}},
            )
        else:
            documents = vector_store.similarity_search_with_score_by_vector(
                embedding, k=body.k, filter={"file_id": {"$in": body.file_ids}}
            )

        # Ensure documents list is not empty
        if not documents:
            raise HTTPException(
                status_code=404, detail="No documents found for the given query"
            )

        return documents
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if debug_mode:
    app.include_router(router=pgvector_router)

if __name__ == "__main__":
    uvicorn.run(app, host=RAG_HOST, port=RAG_PORT, log_config=None)