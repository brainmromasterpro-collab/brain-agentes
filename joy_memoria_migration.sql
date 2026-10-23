-- joy_memoria: eventos de negocio enriquecidos, cross-stream.
-- Joy los lee para tener contexto que 1CRM nunca tendrá:
-- decisiones tomadas, excepciones, patrones emergentes.
-- Corre esto en Supabase > SQL editor.

CREATE TABLE IF NOT EXISTS joy_memoria (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    stream_id   uuid REFERENCES streams(id) ON DELETE SET NULL,
    tipo        text NOT NULL,        -- 'so_creada', 'po_creada', 'pago_registrado', 'cierre_venta', 'decision', 'patron'
    descripcion text NOT NULL,        -- texto humano: "SO-089 creada para Acme Corp por $45,000 USD"
    entidad     text,                 -- nombre del cliente o proveedor
    entidad_id  text,                 -- id en 1CRM
    monto       numeric,
    moneda      text DEFAULT 'USD',
    referencia  text,                 -- "SO-089", "PO-112", "PAY-abc123"
    contexto    jsonb DEFAULT '{}',   -- datos extra estructurados (campos de 1CRM, etc.)
    autor       text DEFAULT 'sistema',  -- 'sistema' (auto) o 'joy' (patrón detectado por Joy)
    created_at  timestamptz DEFAULT now()
);

-- Índices para consultas frecuentes de Joy
CREATE INDEX IF NOT EXISTS joy_memoria_stream_id_idx  ON joy_memoria(stream_id);
CREATE INDEX IF NOT EXISTS joy_memoria_tipo_idx       ON joy_memoria(tipo);
CREATE INDEX IF NOT EXISTS joy_memoria_created_at_idx ON joy_memoria(created_at DESC);
CREATE INDEX IF NOT EXISTS joy_memoria_entidad_idx    ON joy_memoria(entidad);

-- RLS: solo el service role escribe/lee (el worker usa SUPABASE_SERVICE_KEY)
ALTER TABLE joy_memoria ENABLE ROW LEVEL SECURITY;
CREATE POLICY joy_memoria_service_only ON joy_memoria
    USING (auth.role() = 'service_role');
