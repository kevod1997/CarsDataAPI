import base64
import os

from datetime import timedelta

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

# from fastapi.security import OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from pydantic import BaseModel
from slowapi import Limiter
from slowapi.util import get_remote_address

from auth import create_access_token, decode_access_token, get_current_user
from config import (
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    BRAND_ID_COLUMN,
    GOOGLE_CREDENTIALS,
    GROUP_ID_COLUMN,
    JWT_EXPIRATION_MINUTES,
    MODEL_ID_COLUMN,
    NAME_COLUMN,
    SHEET_BRANDS,
    SHEET_DATA,
    SPREADSHEET_ID,
)

# Crear la app de FastAPI
app = FastAPI(
    title="TengoLugarCarsAPI",  # Set custom title
    description="API for consulting DNRPA cars data",  # Optional description
    version="1.0.0",  # Optional version
    openapi_url="/openapi.json",
)


# Configurar CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Añadir un endpoint de health check
@app.get("/health")
async def health_check():
    return {"status": "healthy"}

# Verificar la configuración al inicio
@app.on_event("startup")
async def startup_event():
    print("Starting up...")
    print(f"PORT: {os.getenv('PORT', '8000')}")
    # Verificar otras variables de entorno críticas
    required_vars = ['GOOGLE_CREDENTIALS', 'SPREADSHEET_ID']
    for var in required_vars:
        value = os.getenv(var)
        if not value:
            print(f"Warning: {var} not set!")
        else:
            print(f"{var} is configured")

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
# Configurar directorio de recursos estáticos
app.mount("/static", StaticFiles(directory="static"), name="static")

# Configurar plantillas con Jinja2
templates = Jinja2Templates(directory="templates")


# Modelo del token
class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshTokenRequest(BaseModel):
    refresh_token: str


######## Main Page #########
@app.get("/")
async def redirect_to_login():
    return RedirectResponse(url="/login")


######## Login GET #########
@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_form(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


######## Login POST #########
@app.post("/login", response_model=Token, include_in_schema=False)
@limiter.limit("5/minute")
async def get_access_token(
    request: Request,
    username: str = Form(None),  # Accept form data
    password: str = Form(None),
):
    auth_header = request.headers.get("Authorization")

    # OPTION 1: Basic Auth (if Authorization header exists)
    if auth_header and auth_header.startswith("Basic "):
        try:
            encoded_credentials = auth_header.split(" ")[1]
            decoded_credentials = base64.b64decode(encoded_credentials).decode("utf-8")
            username, password = decoded_credentials.split(":", 1)
        except Exception as e:
            raise HTTPException(
                status_code=400, detail=f"Error decoding credentials: {str(e)}"
            )

    # OPTION 2: Form-based login
    elif not username or not password:
        raise HTTPException(status_code=400, detail="Missing credentials")

    # Validate credentials
    if username != ADMIN_USERNAME or password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Generate tokens
    access_token = create_access_token(
        data={"sub": username},
        expires_delta=timedelta(minutes=JWT_EXPIRATION_MINUTES),
    )
    refresh_token = create_access_token(
        data={"sub": username},
        expires_delta=timedelta(days=1),
    )

    # Verificar el Accept Header para decidir la respuesta
    accept_header = request.headers.get("Accept", "").lower()

    if "text/html" in accept_header:
        # Redirigir a /docs con el token en la URL
        response = RedirectResponse(url=f"/docs?token={access_token}", status_code=303)
        return response

    # Si es una API o WebApp consumiendo JSON, devolver los tokens
    return JSONResponse(
        content={
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "bearer",
        },
        status_code=200,
    )


# Endpoint para refrescar el token de acceso
@app.post("/refresh", response_model=Token, include_in_schema=False)
@limiter.limit("5/minute")
async def refresh_access_token(request: Request, token_data: RefreshTokenRequest):
    # Extract the token from the request body
    token = token_data.refresh_token

    # Decode and validate the refresh token
    payload = decode_access_token(token)
    username = payload.get("sub")
    if not username:
        raise HTTPException(status_code=401, detail="Invalid token")

    # Create a new access token
    new_access_token = create_access_token(
        data={"sub": username},
        expires_delta=timedelta(minutes=JWT_EXPIRATION_MINUTES),
    )

    return {
        "access_token": new_access_token,
        "refresh_token": token,  # Keep the existing refresh token
        "token_type": "bearer",
    }


# Endpoint para consultar datos
@app.get(
    "/brands",
    tags=["Brands"],
    operation_id="getUniqueBrands",
    dependencies=[Depends(get_current_user)],
)
@limiter.limit("5/minute")  # Permite 5 solicitudes por minuto por IP
async def get_unique_brands(
    request: Request,
):  # Agrego request para que slowapi pueda obtener la IP y limitar las solicitudes
    try:
        # Construir servicio
        credentials = Credentials.from_service_account_info(GOOGLE_CREDENTIALS)
        service = build("sheets", "v4", credentials=credentials)
        sheet = service.spreadsheets()
        # Leer datos de la hoja
        result = (
            sheet.values()
            .get(spreadsheetId=SPREADSHEET_ID, range=SHEET_BRANDS)
            .execute()
        )
        values = result.get("values", [])
        if not values:
            return {"message": "No se encontraron datos."}

        # Extraer el índice de la columna "Marcas"
        headers = values[0]  # Primera fila contiene los encabezados
        if NAME_COLUMN not in headers:
            raise HTTPException(
                status_code=500,
                detail=f"Columna '{NAME_COLUMN}' no encontrada en el archivo.",
            )
        elif BRAND_ID_COLUMN not in headers:
            raise HTTPException(
                status_code=500,
                detail=f"Columna '{BRAND_ID_COLUMN}' no encontrada en el archivo.",
            )

        # Obtener los índices de las columnas requeridas
        brand_index = headers.index(NAME_COLUMN)
        id_index = headers.index(BRAND_ID_COLUMN)

        # Procesar las filas restantes y construir la lista de diccionarios
        brands_list = []
        for row in values[1:]:  # Excluir encabezados
            # Evitar errores si la fila no tiene suficientes columnas
            if len(row) > max(brand_index, id_index):
                brand = row[brand_index]
                id_value = int(row[id_index])
                brands_list.append({"id": id_value, "name": brand})
        return {"success": True, "data": brands_list}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get(
    "/groups",
    tags=["Groups"],
    operation_id="getGroupsByBrand",
    dependencies=[Depends(get_current_user)],
)
@limiter.limit("5/minute")  # Permite 5 solicitudes por minuto por IP
async def get_models_by_brand(
    request: Request,
    brandId: int = Query(
        ..., description="ID de la marca para filtrar grupos"
    ),  # Parámetro obligatorio
):
    try:
        # Construir servicio de Google Sheets
        credentials = Credentials.from_service_account_info(GOOGLE_CREDENTIALS)
        service = build("sheets", "v4", credentials=credentials)
        sheet = service.spreadsheets()

        # Leer datos de la hoja
        result = (
            sheet.values().get(spreadsheetId=SPREADSHEET_ID, range=SHEET_DATA).execute()
        )
        values = result.get("values", [])

        if not values:
            return {"message": "No se encontraron datos."}

        # Extraer encabezados
        headers = values[0]

        # Verificar que las columnas requeridas existen
        if "group" not in headers:
            raise HTTPException(
                status_code=400, detail="Columna 'group' no encontrada."
            )
        if "group_id" not in headers:
            raise HTTPException(
                status_code=400, detail="Columna 'group_id' no encontrada."
            )
        if BRAND_ID_COLUMN not in headers:
            raise HTTPException(
                status_code=400, detail=f"Columna '{BRAND_ID_COLUMN}' no encontrada."
            )

        # Obtener los índices de las columnas
        name_index = headers.index("group")
        id_index = headers.index("group_id")
        brand_id_index = headers.index(BRAND_ID_COLUMN)

        # Filtrar modelos según brandId y construir la lista de respuesta
        groups_list = []
        for row in values[1:]:  # Excluir encabezado
            if len(row) > max(name_index, id_index, brand_id_index):
                group_name = row[name_index]
                group_id = row[id_index]  # TODO ver si es necesario convertir a int
                group_brand_id = row[
                    brand_id_index
                ]  # TODO ver si es necesario convertir a int
                if (
                    str(group_brand_id) == str(brandId)
                    and group_id != ""
                    and group_id not in [group["id"] for group in groups_list]
                ):
                    groups_list.append(
                        {"id": group_id, "name": group_name, "brandId": group_brand_id}
                    )

        if not groups_list:
            raise HTTPException(
                status_code=404,
                detail=f"No se encontraron modelos para brandId={brandId}",
            )

        return {"success": True, "data": groups_list}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get(
    "/models",
    tags=["Models"],
    operation_id="getModelsByBrandAndGroup",
    dependencies=[Depends(get_current_user)],
)
@limiter.limit("5/minute")  # Límite de solicitudes por IP
async def get_models_by_brand_and_group(
    request: Request,
    brandId: int = Query(..., description="ID de la marca para filtrar modelos"),
    groupId: int = Query(..., description="ID del grupo para filtrar modelos"),
):
    try:
        # Construir servicio de Google Sheets
        credentials = Credentials.from_service_account_info(GOOGLE_CREDENTIALS)
        service = build("sheets", "v4", credentials=credentials)
        sheet = service.spreadsheets()

        # Leer datos de la hoja
        result = (
            sheet.values().get(spreadsheetId=SPREADSHEET_ID, range=SHEET_DATA).execute()
        )
        values = result.get("values", [])

        if not values:
            return {"message": "No se encontraron datos."}

        # Extraer encabezados
        headers = values[0]

        # Verificar que las columnas requeridas existen
        required_columns = ["model", MODEL_ID_COLUMN, BRAND_ID_COLUMN, GROUP_ID_COLUMN]
        for column in required_columns:
            if column not in headers:
                raise HTTPException(
                    status_code=400, detail=f"Columna '{column}' no encontrada."
                )

        # Obtener los índices de las columnas
        name_index = headers.index("model")
        id_index = headers.index(MODEL_ID_COLUMN)
        brand_id_index = headers.index(BRAND_ID_COLUMN)
        group_id_index = headers.index(GROUP_ID_COLUMN)

        # Filtrar modelos según brandId y groupId
        models_list = []
        for row in values[1:]:  # Excluir encabezado
            if len(row) > max(name_index, id_index, brand_id_index, group_id_index):
                model_name = row[name_index]
                model_id = row[id_index]
                model_brand_id = row[brand_id_index]
                model_group_id = row[group_id_index]

                # Filtrar por brandId y groupId
                if (
                    str(model_brand_id) == str(brandId)
                    and str(model_group_id) == str(groupId)
                    and model_id != ""
                    and model_id not in [model["id"] for model in models_list]
                ):
                    models_list.append(
                        {
                            "id": int(model_id),
                            "name": model_name,
                            "brandId": int(model_brand_id),
                            "groupId": int(model_group_id),
                        }
                    )

        if not models_list:
            raise HTTPException(
                status_code=404,
                detail=f"No se encontraron modelos para brandId={brandId} y groupId={groupId}",
            )

        return {"success": True, "data": models_list}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get(
    "/models/{id}",
    tags=["Models"],
    operation_id="getModelDetails",
    dependencies=[Depends(get_current_user)],
)
@limiter.limit("5/minute")  # Límite de solicitudes por IP
async def get_model_details(request: Request, id: int):
    try:
        # Construir servicio de Google Sheets
        credentials = Credentials.from_service_account_info(GOOGLE_CREDENTIALS)
        service = build("sheets", "v4", credentials=credentials)
        sheet = service.spreadsheets()

        # Leer datos de la hoja
        result = (
            sheet.values().get(spreadsheetId=SPREADSHEET_ID, range=SHEET_DATA).execute()
        )
        values = result.get("values", [])

        if not values:
            return {"message": "No se encontraron datos."}

        # Extraer encabezados
        headers = values[0]
        rows = values[1:]

        # Crear un mapeo dinámico entre encabezados y valores
        for row in rows:
            row_data = dict(zip(headers, row))  # Combinar encabezados con valores

            # Verificar si el modelo coincide con el ID solicitado
            if str(row_data.get(MODEL_ID_COLUMN)) == str(id):
                # Construir la respuesta
                try:
                    fuel_efficiency = row_data.get("fuel_efficiency", "0")
                    if fuel_efficiency:
                        fuel_efficiency = float(fuel_efficiency.replace(",", "."))
                    else:
                        fuel_efficiency = (
                            0.0  # Valor por defecto si está vacío o no numérico
                        )
                except ValueError:
                    fuel_efficiency = (
                        0.0  # Valor por defecto en caso de error de conversión
                    )

                fuel_type = (
                    row_data.get("fuel_type", "").strip().lower()
                )  # Usar strip() por si hay espacios extra

                return {
                    "success": True,
                    "data": {
                        "id": int(row_data[MODEL_ID_COLUMN]),
                        "name": row_data["model"],
                        "fuelType": fuel_type,  # Usamos la variable con manejo seguro
                        "fuelEfficiency": fuel_efficiency,
                        "brand": {
                            "id": int(row_data[BRAND_ID_COLUMN]),
                            "name": row_data["brand"],
                        },
                        "group": {
                            "id": int(row_data[GROUP_ID_COLUMN]),
                            "name": row_data["group"],
                        },
                    },
                }

        # Si no se encuentra el modelo
        raise HTTPException(
            status_code=404, detail=f"Modelo con id={id} no encontrado."
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# Endpoint para hacer un post en GoogleSheets (Data sheet) a partir de model_id (posteando fuel_type y fuel_efficiency)
@app.post(
    "/models/{id}",
    tags=["Models"],
    operation_id="updateModelDetails",
    dependencies=[Depends(get_current_user)],
)
@limiter.limit("5/minute")  # Límite de solicitudes por IP
async def update_model_details(
    request: Request,
    id: int,
    fuelType: str = Query(None, description="Tipo de combustible"),
    fuelEfficiency: float = Query(None, description="Eficiencia de combustible"),
):
    try:
        # Construir servicio de Google Sheets
        credentials = Credentials.from_service_account_info(GOOGLE_CREDENTIALS)
        service = build("sheets", "v4", credentials=credentials)
        sheet = service.spreadsheets()

        # Leer datos de la hoja
        result = (
            sheet.values().get(spreadsheetId=SPREADSHEET_ID, range=SHEET_DATA).execute()
        )
        values = result.get("values", [])

        if not values:
            return {"message": "No se encontraron datos."}

        # Extraer encabezados
        headers = values[0]
        rows = values[1:]

        # Crear un mapeo dinámico entre encabezados y valores
        for row in rows:
            row_data = dict(zip(headers, row))  # Combinar encabezados con valores

            # Verificar si el modelo coincide con el ID solicitado
            if str(row_data.get(MODEL_ID_COLUMN)) == str(id):
                # Asegúrate de que fuelType y fuelEfficiency no sean None
                if fuelType is not None:
                    row_data["fuel_type"] = fuelType
                else:
                    return {"success": False, "message": "fuelType no puede ser None"}

                if fuelEfficiency is not None:
                    row_data["fuel_efficiency"] = str(fuelEfficiency)
                else:
                    return {
                        "success": False,
                        "message": "fuelEfficiency no puede ser None",
                    }

                # Actualizar la fila en Google Sheets
                body = {"values": [list(row_data.values())]}
                result = (
                    sheet.values()
                    .update(
                        spreadsheetId=SPREADSHEET_ID,
                        range=f"{SHEET_DATA}!A{rows.index(row) + 2}",
                        valueInputOption="USER_ENTERED",  # Para que GoogleSheets interprete los valores como float
                        body=body,
                    )
                    .execute()
                )

                return {"success": True, "message": "Modelo actualizado correctamente."}

        # Si no se encuentra el modelo
        raise HTTPException(
            status_code=404, detail=f"Modelo con id={id} no encontrado."
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
