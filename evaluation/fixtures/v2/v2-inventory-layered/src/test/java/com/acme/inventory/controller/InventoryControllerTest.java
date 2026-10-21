package com.acme.inventory.controller;

import static org.junit.Assert.assertEquals;
import org.junit.Test;

public class InventoryControllerTest {
    @Test
    public void reservesStock() {
        assertEquals(7, new InventoryController().reserve("sku-1", 3));
    }
}
