package com.acme.inventory.service;

import com.acme.inventory.repository.StockRepository;

public class InventoryService {
    private final StockRepository repository;
    public InventoryService(StockRepository repository) { this.repository = repository; }
    public int reserve(String id, int qty) {
        return repository.reserve(id, qty);
    }
}
