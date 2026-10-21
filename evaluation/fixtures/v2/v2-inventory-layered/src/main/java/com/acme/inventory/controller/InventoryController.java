package com.acme.inventory.controller;

import com.acme.inventory.repository.StockRepository;
import com.acme.inventory.service.InventoryService;

public class InventoryController {
    private final InventoryService service = new InventoryService(new StockRepository());
    public int reserve(String id, int qty) { return service.reserve(id, qty); }
}
