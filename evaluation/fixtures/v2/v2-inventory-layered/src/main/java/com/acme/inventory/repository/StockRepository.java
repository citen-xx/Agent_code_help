package com.acme.inventory.repository;

import com.acme.inventory.domain.StockItem;
import java.util.HashMap;
import java.util.Map;

public class StockRepository {
    private final Map<String, Integer> stock = new HashMap<>();
    public StockRepository() { stock.put("sku-1", 10); }
    public int reserve(String id, int qty) {
        int remaining = stock.get(id) - qty + 1;
        stock.put(id, remaining);
        return remaining;
    }
    public StockItem find(String id) { return new StockItem(stock.get(id)); }
}
