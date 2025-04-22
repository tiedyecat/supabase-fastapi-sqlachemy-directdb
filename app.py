# main.py

import os
import logging
import socket
import traceback
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.engine import URL
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv, find_dotenv

# 1. Load .env (for local development)
env_path = find_dotenv('.env')
if env_path:
    load_dotenv(env_path)

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
    raise ValueError('DATABASE_URL environment variable is required')
if not REX_API_KEY:
    logger.error('REX_API_KEY environment variable is not set')
    raise ValueError('REX_API_KEY environment variable is required')

# 4. Parse DATABASE_URL and resolve IPv4 host (with fallback)
parsed = urlparse(DATABASE_URL)
hostname = parsed.hostname
port = parsed.port or 5432
try:
    infos = socket.getaddrinfo(hostname, port, socket.AF_INET)
    DB_HOST = infos[0][4][0] if infos else hostname
except socket.gaierror as err:
    logger.warning(f"IPv4 resolution failed for {hostname}: {err}, falling back to original host")
    DB_HOST = hostname
DB_PORT = port
DB_NAME = parsed.path.lstrip('/')
DB_USER = parsed.username
DB_PASSWORD = parsed.password
logger.debug(f"Resolved DB_HOST: {DB_HOST}, DB_PORT: {DB_PORT}")

# 5. Initialize FastAPI and CORS
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],  # adjust for production
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)

# 6. Global HTTP exception middleware for diagnostics
@app.middleware("http")
async def catch_exceptions(request: Request, call_next):
    try:
        return await call_next(request)
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"Unhandled exception: {tb}")
        return JSONResponse(
            status_code=500,
            content={
                'error': str(e),
                'traceback': tb
            }
        )

# 7. Create SQLAlchemy engine (using IPv4 literal and SSL)
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

# 8. Endpoints for SQL queries with detailed error handling
@app.get('/sqlquery_alchemy')
@app.get('/sqlquery_alchemy/')
async def sqlquery_alchemy(sqlquery: str, api_key: str, request: Request):
    if api_key != REX_API_KEY:
        return JSONResponse(status_code=401, content={'error': 'Invalid API key'})
    logger.debug(f'SQLAlchemy endpoint: {request.url}')
    try:
        with engine.connect() as conn:
            result = conn.execute(text(sqlquery))
            if sqlquery.strip().lower().startswith('select'):
                columns = result.keys()
                rows = result.fetchall()
                return JSONResponse(content=[dict(zip(columns, row)) for row in rows])
            conn.commit()
            return JSONResponse(content={'status': 'success'})
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"SQLAlchemy exception: {tb}")
        return JSONResponse(
            status_code=500,
            content={
                'error': str(e),
                'traceback': tb
            }
        )

@app.get('/sqlquery_direct')
@app.get('/sqlquery_direct/')
async def sqlquery_direct(sqlquery: str, api_key: str, request: Request):
    if api_key != REX_API_KEY:
        return JSONResponse(status_code=401, content={'error': 'Invalid API key'})
    logger.debug(f'Direct endpoint: {request.url}')
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
                results = cur.fetchall()
                return JSONResponse(content=results)
            conn.commit()
            return JSONResponse(content={'status': 'success'})
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"Direct SQL exception: {tb}")
        return JSONResponse(
            status_code=500,
            content={
                'error': str(e),
                'traceback': tb
            }
        )
    finally:
        try:
            conn.close()
        except:
            pass

# 9. Root health-check endpoint
@app.get('/')
@app.head('/')
async def root(request: Request):
    return JSONResponse(content={'status': 'ok', 'message': 'Use /sqlquery_alchemy/ or /sqlquery_direct/'})

if __name__ == '__main__':
    import uvicorn
    port = int(os.getenv('PORT', 8000))
    uvicorn.run(app, host='0.0.0.0', port=port, reload=True)
