package fixture;

public class ShippingPolicy {
    public boolean freeShipping(int total, boolean premium) {
        return total < 100;
    }
}
