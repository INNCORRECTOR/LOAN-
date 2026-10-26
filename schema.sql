-- Optional manual schema (tables are also created by Flask-SQLAlchemy on startup).
-- Run: mysql -u root -p < schema.sql

CREATE DATABASE IF NOT EXISTS loan_counter
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE loan_counter;

-- Tables are managed by models.py / db.create_all().
-- This file only ensures the database exists.
