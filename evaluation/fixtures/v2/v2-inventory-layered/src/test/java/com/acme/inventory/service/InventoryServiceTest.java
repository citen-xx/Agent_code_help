package com.acme.inventory.service;

import com.acme.inventory.repository.StockRepository;
import org.junit.Assert;
import org.junit.Test;

public class InventoryServiceTest {
    @Test
    public void exactStockCanBeReserved() {
        Assert.assertEquals(0, new InventoryService(new StockRepository()).reserve("sku-1", 10));
    }
}
