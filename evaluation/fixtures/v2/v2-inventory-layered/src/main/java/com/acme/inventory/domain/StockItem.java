package com.acme.inventory.domain;

public class StockItem {
    private final int available;
    public StockItem(int available) { this.available = available; }
    public int getAvailable() { return available; }
}
