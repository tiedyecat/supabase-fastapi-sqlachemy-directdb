import os
import json
import logging
import socket
import traceback
import re
from urllib.parse import urlparse
from datetime import datetime, date

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import SQLAlchemyError
import psycopg2
from psycopg2 import OperationalError, ProgrammingError
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv, find_dotenv

# 1. Load .env (for local development)
env_path = find_dotenv('.env')
if env_path:
    load_dotenv(env_path)
    logging.getLogger(__name__).info(f"Loaded environment from {env_path}")

# 2. Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger(__name__)

# 3. Read & validate environment variables
DATABASE_URL = os.getenv('DATABASE_URL')
REX_API_KEY = os.getenv('REX_API_KEY')
if not DATABASE_URL:
    logger.error('DATABASE_URL environment variable is not set')
    raise RuntimeError('DATABASE_URL environment variable is required')
if not REX_API_KEY:
    logger.error('REX_API_KEY environment variable is not set')
    raise RuntimeError('REX_API_KEY environment variable is required')

# Validate API key format and correctness
def validate_api_key(key: str):
    pattern = re.compile(r'^[A-Za-z0-9%]+$')
    if not pattern.match(key):
        raise HTTPException(status_code=400, detail="Invalid API key format")
    if key != REX_API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized: invalid API key")
    return key

# 4. Parse DATABASE_URL and resolve IPv4 host (with fallback)
parsed = urlparse(DATABASE_URL)
hostname = parsed.hostname
port = parsed.port or 5432
try:
    infos = socket.getaddrinfo(hostname, port, socket.AF_INET)
    DB_HOST = infos[0][4][0] if infos else hostname
except socket.gaierror as err:
    logger.warning(f"IPv4 resolution failed for {hostname}: {err}, falling back to hostname")
    DB_HOST = hostname
DB_PORT = port
DB_NAME = parsed.path.lstrip('/')
DB_USER = parsed.username
DB_PASSWORD = parsed.password
logger.debug(f"Resolved DB_HOST: {DB_HOST}, DB_PORT: {DB_PORT}")

# 5. Initialize FastAPI (disable auto-generated OpenAPI)
app = FastAPI(
    openapi_url=None,
    docs_url="/docs",
    redoc_url=None
)

# 5a. Serve plugin manifest and OpenAPI spec
app.mount(
    "/.well-known",
    StaticFiles(directory=".well-known"),
    name="well-known"
)

# 5b. Override OpenAPI schema to use custom spec
def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    try:
        with open("openapi_spec.json", "r") as f:
            spec = json.load(f)
    except FileNotFoundError:
        logger.error("OpenAPI spec file not found: openapi_spec.json")
        raise RuntimeError("Missing openapi_spec.json file")
    except json.JSONDecodeError as e:
        logger.error(f"Error parsing openapi_spec.json: {e}")
        raise RuntimeError("Invalid JSON in openapi_spec.json")
    app.openapi_schema = spec
    return app.openapi_schema

app.openapi = custom_openapi

# 5c. Apply CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # adjust for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 6. Exception handlers
@app.exception_handler(StarletteHTTPException)
async def starlette_http_exception_handler(request: Request, exc: StarletteHTTPException):
    logger.warning(f"HTTP Exception: {exc.detail}")
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})

@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    tb = traceback.format_exc()
    logger.error(f"Unhandled exception: {tb}")
    return JSONResponse(status_code=500, content={"error": "Internal server error", "details": str(exc)})

# 7. Create SQLAlchemy engine (IPv4 + SSL)
try:
    engine_url = URL.create(
        drivername='postgresql+psycopg2',
        username=DB_USER,
        password=DB_PASSWORD,
        host=DB_HOST,
        port=DB_PORT,
        database=DB_NAME,
        query={'sslmode': 'require'},
    )
    engine = create_engine(engine_url, pool_pre_ping=True)
    @app.on_event("startup")
    def check_db_connection():
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            logger.info("Database connection verified.")
        except SQLAlchemyError as e:
            logger.error(f"Database startup check failed (continuing anyway): {e}")
except Exception as e:
    logger.critical(f"Engine creation error: {e}")
    raise

# 8. SQLAlchemy endpoint (patched with datetime serialization)
@app.get('/sqlquery_alchemy')
@app.get('/sqlquery_alchemy/')
async def sqlquery_alchemy(sqlquery: str, request: Request, api_key: str = Depends(validate_api_key)):
    if len(sqlquery) > 5000:
        raise HTTPException(status_code=413, detail="SQL query too long")
    logger.debug(f"SQLAlchemy query: {sqlquery}")
    try:
        with engine.connect() as conn:
            result = conn.execute(text(sqlquery))
            if sqlquery.strip().lower().startswith('select'):
                cols = result.keys()
                rows = result.fetchall()

                # Safe serialization of datetime/date
                def serialize_row(row):
                    return {
                        col: (val.isoformat() if isinstance(val, (datetime, date)) else val)
                        for col, val in zip(cols, row)
                    }

                return JSONResponse(content=[serialize_row(r) for r in rows])
            conn.commit()
            return JSONResponse(content={"status": "success"})
    except ProgrammingError as e:
        logger.error(f"SQL syntax error: {e}")
        raise HTTPException(status_code=400, detail="Invalid SQL syntax")
    except OperationalError as e:
        logger.error(f"Operational error: {e}")
        raise HTTPException(status_code=500, detail="Database operational error")
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"Unexpected error: {tb}")
        raise HTTPException(status_code=500, detail="Internal server error")

# 9. Direct psycopg2 endpoint
@app.get('/sqlquery_direct')
@app.get('/sqlquery_direct/')
async def sqlquery_direct(sqlquery: str, request: Request, api_key: str = Depends(validate_api_key)):
    if len(sqlquery) > 5000:
        raise HTTPException(status_code=413, detail="SQL query too long")
    logger.debug(f"Direct SQL query: {sqlquery}")
    conn = None
    try:
        conn = psycopg2.connect(
            host=DB_HOST,
            port=DB_PORT,
            dbname=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD,
            sslmode='require',
            cursor_factory=RealDictCursor
        )
        with conn.cursor() as cur:
            cur.execute(sqlquery)
            if sqlquery.strip().lower().startswith('select'):
                rows = cur.fetchall()
                return JSONResponse(content=rows)
            conn.commit()
            return JSONResponse(content={"status": "success"})
    except ProgrammingError as e:
        logger.error(f"SQL syntax error: {e}")
        raise HTTPException(status_code=400, detail="Invalid SQL syntax")
    except OperationalError as e:
        logger.error(f"Operational error: {e}")
        raise HTTPException(status_code=500, detail="Database operational error")
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"Unexpected error: {tb}")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn:
            conn.close()

# 10. Health-check endpoint
@app.get('/')
@app.head('/')
async def root(request: Request):
    return JSONResponse(content={"status": "ok", "message": "Use /sqlquery_alchemy/ or /sqlquery_direct/"})

# 11. Start the server
if __name__ == '__main__':
    import uvicorn
    port = int(os.getenv('PORT', 8000))
    uvicorn.run('app:app', host='0.0.0.0', port=port, reload=True)