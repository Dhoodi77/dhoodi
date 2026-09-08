-- PostgreSQL initialization script for TradeOS
-- Creates the tradeos database with proper permissions and character set

-- Connect to the default postgres database
\c postgres;

-- Drop existing tradeos database if it exists (for clean reinstall)
-- Uncomment for fresh deployment, but commented by default to preserve data
-- DROP DATABASE IF EXISTS tradeos;

-- The database is already created by POSTGRES_DB environment variable,
-- but we ensure proper settings here

ALTER DATABASE tradeos SET timezone = 'UTC';
ALTER DATABASE tradeos SET client_encoding = 'UTF8';

-- Connect to tradeos database
\c tradeos;

-- Create schema
CREATE SCHEMA IF NOT EXISTS public;

-- Extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- Set default privileges
GRANT ALL PRIVILEGES ON SCHEMA public TO tradeos;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO tradeos;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO tradeos;

-- Create user-defined types if needed
CREATE TYPE trading_mode AS ENUM ('paper', 'live', 'development');
CREATE TYPE position_status AS ENUM ('open', 'closed');
CREATE TYPE trade_side AS ENUM ('buy', 'sell');
CREATE TYPE trade_status AS ENUM ('pending', 'filled', 'failed', 'cancelled');
CREATE TYPE opportunity_status AS ENUM ('detected', 'analyzing', 'approved', 'rejected', 'executed', 'expired');

GRANT USAGE ON TYPE trading_mode TO tradeos;
GRANT USAGE ON TYPE position_status TO tradeos;
GRANT USAGE ON TYPE trade_side TO tradeos;
GRANT USAGE ON TYPE trade_status TO tradeos;
GRANT USAGE ON TYPE opportunity_status TO tradeos;
