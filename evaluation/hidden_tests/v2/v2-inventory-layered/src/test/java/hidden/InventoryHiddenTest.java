package hidden;

import com.acme.inventory.controller.InventoryController;
import org.junit.Test;

public class InventoryHiddenTest {
    @Test(expected = IllegalStateException.class)
    public void cannotOversell() {
        new InventoryController().reserve("sku-1", 11);
    }
}
