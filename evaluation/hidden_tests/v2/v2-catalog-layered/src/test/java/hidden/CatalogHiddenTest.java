package hidden;

import com.acme.catalog.controller.CatalogController;
import org.junit.Assert;
import org.junit.Test;

public class CatalogHiddenTest {
    @Test
    public void handlesLargerQuantity() {
        Assert.assertEquals(330, new CatalogController().quote("book", 3));
    }
}
