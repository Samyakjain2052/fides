CREATE TABLE IF NOT EXISTS support_tickets (
    id             INT AUTO_INCREMENT PRIMARY KEY,
    customer_email VARCHAR(255) NULL,
    customer_name  VARCHAR(255) NULL,
    phone          VARCHAR(64) NULL,
    subject        VARCHAR(255) NULL,
    created_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_support_tickets_email ON support_tickets (customer_email);

INSERT INTO support_tickets (customer_email, customer_name, phone, subject, created_at) VALUES
    ('demo@example.com', 'Demo Person', '+1-555-0100', 'Question about my order', '2025-04-28 10:00:00'),
    ('demo@example.com', 'Demo Person', '+1-555-0100', 'Delivery status request', '2025-04-29 11:30:00'),
    ('control@example.com', 'Control Person', '+1-555-0199', 'Product question', '2025-04-30 09:15:00');
